package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// Drop traffic without closing the TCP connection, including HTTP/2 control
// frames. A sleeping HTTP handler would leave the connection itself healthy.
type heartbeatBlackholeConn struct {
	net.Conn
	drop atomic.Bool
}

func (c *heartbeatBlackholeConn) Read(p []byte) (int, error) {
	for {
		n, err := c.Conn.Read(p)
		if err != nil || !c.drop.Load() {
			return n, err
		}
	}
}

func (c *heartbeatBlackholeConn) Write(p []byte) (int, error) {
	if c.drop.Load() {
		return len(p), nil
	}
	return c.Conn.Write(p)
}

type heartbeatFaultListener struct {
	net.Listener
	mu    sync.Mutex
	conns []*heartbeatBlackholeConn
}

type heartbeatHeadersTransport struct {
	*http.Transport
	seen chan struct{}
	once sync.Once
}

func (t *heartbeatHeadersTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	response, err := t.Transport.RoundTrip(r)
	if err == nil {
		t.once.Do(func() { close(t.seen) })
	}
	return response, err
}

func (l *heartbeatFaultListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	wrapped := &heartbeatBlackholeConn{Conn: conn}
	l.mu.Lock()
	l.conns = append(l.conns, wrapped)
	l.mu.Unlock()
	return wrapped, nil
}

func (l *heartbeatFaultListener) connections() []*heartbeatBlackholeConn {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]*heartbeatBlackholeConn(nil), l.conns...)
}

func (l *heartbeatFaultListener) forRequest(r *http.Request) *heartbeatBlackholeConn {
	for _, conn := range l.connections() {
		if conn.RemoteAddr().String() == r.RemoteAddr {
			return conn
		}
	}
	return nil
}

func heartbeatTransportServer(t *testing.T, http2 bool, handler http.Handler) (*httptest.Server, *heartbeatFaultListener) {
	t.Helper()
	server := httptest.NewUnstartedServer(handler)
	listener := &heartbeatFaultListener{Listener: server.Listener}
	server.Listener = listener
	server.EnableHTTP2 = http2
	if http2 {
		server.StartTLS()
	} else {
		server.Start()
	}
	t.Cleanup(server.Close)
	return server, listener
}

func heartbeatTransportConfig(t *testing.T, server *httptest.Server, reuse bool) *Config {
	t.Helper()
	client := newHeartbeatHTTPClient()
	transport := client.Transport.(*http.Transport)
	if reuse {
		// Negative control: restore the old connection reuse behavior to prove
		// that the blackhole fixture exercises the regression.
		transport.DisableKeepAlives = false
	}
	if server.TLS != nil {
		roots := x509.NewCertPool()
		roots.AddCert(server.Certificate())
		transport.TLSClientConfig = &tls.Config{RootCAs: roots}
	}
	client.Timeout = 250 * time.Millisecond
	previousClient, previousStatus, previousDiscovery := heartbeatHTTPClient, lastReportedStatus, domainDiscovery
	heartbeatHTTPClient = client
	lastReportedStatus = "before-heartbeat"
	domainDiscovery = &discoveryController{}
	t.Cleanup(func() {
		client.CloseIdleConnections()
		heartbeatHTTPClient, lastReportedStatus, domainDiscovery = previousClient, previousStatus, previousDiscovery
	})
	return &Config{
		ServerURL: server.URL, AgentID: "heartbeat-test", Token: "heartbeat-private-token",
		ServiceType: "oxidns", ServiceManager: "command", StatusCommand: "exit 0",
	}
}

func assertHeartbeatProtocol(t *testing.T, got int32, http2 bool) {
	t.Helper()
	want := int32(1)
	if http2 {
		want = 2
	}
	if got != want {
		t.Fatalf("HTTP protocol major = %d, want %d", got, want)
	}
}

func TestHeartbeatHTTPClientOwnsTransport(t *testing.T) {
	defaultTransport := http.DefaultTransport.(*http.Transport)
	previousKeepAlives := defaultTransport.DisableKeepAlives
	client := newHeartbeatHTTPClient()
	t.Cleanup(client.CloseIdleConnections)
	transport, ok := client.Transport.(*http.Transport)
	if !ok || transport == defaultTransport {
		t.Fatal("heartbeat client must own a separate HTTP transport")
	}
	if !transport.DisableKeepAlives || client.Timeout != 10*time.Second {
		t.Fatal("heartbeat client must use fresh connections with a 10 second timeout")
	}
	if defaultTransport.DisableKeepAlives != previousKeepAlives {
		t.Fatal("heartbeat constructor modified the shared default transport")
	}
}

func TestHeartbeatDoesNotReuseBlackholedConnection(t *testing.T) {
	for _, protocol := range []struct {
		name  string
		http2 bool
	}{{"HTTP1", false}, {"HTTP2", true}} {
		t.Run(protocol.name, func(t *testing.T) {
			for _, reuse := range []bool{false, true} {
				name := "fresh_connections"
				if reuse {
					name = "reused_connection_negative_control"
				}
				t.Run(name, func(t *testing.T) {
					var observedProtocol atomic.Int32
					server, listener := heartbeatTransportServer(t, protocol.http2, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						observedProtocol.Store(int32(r.ProtoMajor))
						_, _ = io.WriteString(w, `{}`)
					}))
					cfg := heartbeatTransportConfig(t, server, reuse)
					if err := cfg.sendHeartbeatRequest([]byte(`{}`)); err != nil {
						t.Fatalf("first heartbeat failed: %v", err)
					}
					assertHeartbeatProtocol(t, observedProtocol.Load(), protocol.http2)
					connections := listener.connections()
					if len(connections) != 1 {
						t.Fatalf("first heartbeat opened %d connections", len(connections))
					}
					connections[0].drop.Store(true)
					started := time.Now()
					err := cfg.sendHeartbeatRequest([]byte(`{}`))
					if reuse {
						var timeout net.Error
						if !errors.As(err, &timeout) || !timeout.Timeout() {
							t.Fatalf("reused blackholed connection must time out, got %v", err)
						}
						if len(listener.connections()) != 1 {
							t.Fatal("negative control did not reuse the existing connection")
						}
					} else {
						if err != nil {
							t.Fatalf("next heartbeat did not recover on a fresh connection: %v", err)
						}
						if len(listener.connections()) != 2 {
							t.Fatal("next heartbeat did not open a fresh connection")
						}
					}
					if time.Since(started) > 2*time.Second {
						t.Fatal("heartbeat exceeded its bounded request timeout")
					}
				})
			}
		})
	}
}

func TestHeartbeatRecoversAfterInterruptedResponse(t *testing.T) {
	for _, protocol := range []struct {
		name  string
		http2 bool
	}{{"HTTP1", false}, {"HTTP2", true}} {
		t.Run(protocol.name, func(t *testing.T) {
			for _, stage := range []string{"headers", "body", "body_after_complete_json", "truncated_body", "truncated_complete_json", "oversized_body"} {
				t.Run(stage, func(t *testing.T) {
					var calls, observedProtocol atomic.Int32
					var listener *heartbeatFaultListener
					release := make(chan struct{})
					headersReceived := make(chan struct{})
					server, accepted := heartbeatTransportServer(t, protocol.http2, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						observedProtocol.Store(int32(r.ProtoMajor))
						if calls.Add(1) != 1 {
							_, _ = io.WriteString(w, `{}`)
							return
						}
						if strings.HasPrefix(stage, "truncated_") {
							w.Header().Set("Content-Length", "128")
							body := `{"domain_discovery_enabled":`
							if stage == "truncated_complete_json" {
								body = `{}`
							}
							_, _ = io.WriteString(w, body)
							return
						}
						if stage == "oversized_body" {
							_, _ = io.WriteString(w, `{}`+strings.Repeat(" ", 64*1024))
							return
						}
						conn := listener.forRequest(r)
						if conn == nil {
							t.Error("request connection was not recorded")
							return
						}
						if stage == "headers" {
							conn.drop.Store(true)
						}
						body := `{"domain_discovery_enabled":`
						if stage == "body_after_complete_json" {
							body = `{}`
						}
						_, _ = io.WriteString(w, body)
						w.(http.Flusher).Flush()
						if stage != "headers" {
							// HTTP/2 Flush can return before frames reach the client.
							// Do not blackhole a body test until Do can see headers.
							select {
							case <-headersReceived:
							case <-release:
								return
							}
						}
						conn.drop.Store(true)
						<-release
					}))
					listener = accepted
					// Release handlers before httptest waits for their connections.
					t.Cleanup(func() { close(release) })
					cfg := heartbeatTransportConfig(t, server, false)
					heartbeatHTTPClient.Transport = &heartbeatHeadersTransport{
						Transport: heartbeatHTTPClient.Transport.(*http.Transport), seen: headersReceived,
					}
					var discoveryStopped bool
					domainDiscovery.cancel = func() { discoveryStopped = true }
					started := time.Now()
					err := cfg.sendHeartbeatRequest([]byte(`{}`))
					if err == nil {
						t.Fatal("interrupted heartbeat response was reported as successful")
					}
					if strings.Contains(err.Error(), cfg.Token) {
						t.Fatal("heartbeat error exposed the Agent token")
					}
					if strings.HasPrefix(stage, "truncated_") {
						if !errors.Is(err, io.ErrUnexpectedEOF) {
							t.Fatalf("truncated response lost its underlying read error: %v", err)
						}
					} else if stage != "oversized_body" {
						var timeout net.Error
						if !errors.As(err, &timeout) || !timeout.Timeout() {
							t.Fatalf("blackholed response must time out, got %v", err)
						}
					}
					if stage != "headers" && stage != "oversized_body" && !strings.Contains(err.Error(), "failed to read heartbeat response from") {
						t.Fatalf("response body failure was misclassified: %v", err)
					}
					if time.Since(started) > 2*time.Second {
						t.Fatal("interrupted response exceeded its bounded timeout")
					}
					assertHeartbeatProtocol(t, observedProtocol.Load(), protocol.http2)
					if discoveryStopped || lastReportedStatus != "before-heartbeat" {
						t.Fatal("failed heartbeat updated discovery or reported service status")
					}
					if err := cfg.sendHeartbeatRequest([]byte(`{}`)); err != nil {
						t.Fatalf("next heartbeat did not recover: %v", err)
					}
					if len(listener.connections()) != 2 {
						t.Fatal("recovery heartbeat did not use a new connection")
					}
					if !discoveryStopped || lastReportedStatus == "before-heartbeat" {
						t.Fatal("successful recovery did not apply heartbeat response state")
					}
				})
			}
		})
	}
}

type heartbeatErrorReader struct{ err error }

func (r heartbeatErrorReader) Read([]byte) (int, error) { return 0, r.err }

func TestHeartbeatResponseReadErrorKeepsCausePrivate(t *testing.T) {
	const token = "heartbeat-read-private-token"
	want := errors.New("underlying response failure containing " + token)
	previousClient := heartbeatHTTPClient
	heartbeatHTTPClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(heartbeatErrorReader{err: want})}, nil
	})}
	t.Cleanup(func() { heartbeatHTTPClient = previousClient })
	cfg := &Config{ServerURL: "https://heartbeat.example", AgentID: "test", Token: token}
	err := cfg.sendHeartbeatRequest([]byte(`{}`))
	if !errors.Is(err, want) {
		t.Fatalf("response read error did not preserve its cause: %v", err)
	}
	if strings.Contains(err.Error(), token) || strings.Contains(err.Error(), "underlying response failure") {
		t.Fatal("response read error exposed its raw cause")
	}
}

func TestHeartbeatAcceptsLegacyEmptyResponse(t *testing.T) {
	for _, http2 := range []bool{false, true} {
		for _, response := range []struct {
			name string
			body string
		}{{"empty", ""}, {"object", `{}`}, {"whitespace", " \n\r\t "}} {
			name := "HTTP1/" + response.name
			if http2 {
				name = "HTTP2/" + response.name
			}
			t.Run(name, func(t *testing.T) {
				server, _ := heartbeatTransportServer(t, http2, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					_, _ = io.WriteString(w, response.body)
				}))
				cfg := heartbeatTransportConfig(t, server, false)
				if err := cfg.sendHeartbeatRequest([]byte(`{}`)); err != nil {
					t.Fatalf("legacy heartbeat response rejected: %v", err)
				}
			})
		}
	}
}
