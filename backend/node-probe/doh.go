// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/metacubex/mihomo/component/dialer"
	"github.com/metacubex/mihomo/component/resolver"
	D "github.com/miekg/dns"
)

var errDoHResponse = errors.New("invalid encrypted DNS response")

type probeDNSClient interface {
	ExchangeContext(context.Context, *D.Msg) (*D.Msg, error)
	ResetConnection()
}

// Mihomo's DoH client follows redirects and does not expose its HTTP client.
// Keep a small RFC 8484 transport here so a selected HTTPS resolver cannot
// redirect a query to plaintext HTTP or to another unconfigured provider.
type probeDoHClient struct {
	endpoint string
	client   *http.Client
}

func newProbeDoHClient(endpoint string, bootstrap resolver.Resolver) *probeDoHClient {
	transport := &http.Transport{
		// Proxy remains nil: environment HTTP proxies must not change DNS paths.
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			return dialer.DialContext(ctx, network, address, dialer.WithResolver(bootstrap))
		},
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout: 5 * time.Second,
		ForceAttemptHTTP2:   true,
		DisableCompression:  true,
	}
	return &probeDoHClient{endpoint: endpoint, client: &http.Client{
		Transport: transport,
		Timeout:   5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("encrypted DNS redirects are not allowed")
		},
	}}
}

func (c *probeDoHClient) ResetConnection() { c.client.CloseIdleConnections() }

func (c *probeDoHClient) ExchangeContext(ctx context.Context, query *D.Msg) (*D.Msg, error) {
	message := query.Copy()
	message.Id = 0
	packet, err := message.Pack()
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(packet))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/dns-message")
	req.Header.Set("Accept", "application/dns-message")
	response, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	mediaType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if response.StatusCode != http.StatusOK || err != nil || mediaType != "application/dns-message" {
		return nil, errDoHResponse
	}
	packet, err = io.ReadAll(io.LimitReader(response.Body, 65536))
	if err != nil {
		return nil, err
	}
	if len(packet) > 65535 {
		return nil, errDoHResponse
	}
	var answer D.Msg
	if err := answer.Unpack(packet); err != nil {
		return nil, errDoHResponse
	}
	if !answer.Response || answer.Id != 0 || len(answer.Question) != len(query.Question) {
		return nil, errDoHResponse
	}
	for i, question := range answer.Question {
		wanted := query.Question[i]
		if question.Qtype != wanted.Qtype || question.Qclass != wanted.Qclass || !strings.EqualFold(question.Name, wanted.Name) {
			return nil, errDoHResponse
		}
	}
	answer.Id = query.Id
	return &answer, nil
}

// Preserve the existing first-success behavior when users mix ordinary DNS,
// DoT and DoH. The outer Mihomo resolver still owns caching and A/AAAA handling.
type probeDNSClients []probeDNSClient

func (probeDNSClients) Address() string { return "node-test-dns" }
func (clients probeDNSClients) ResetConnection() {
	for _, client := range clients {
		client.ResetConnection()
	}
}

func (clients probeDNSClients) ExchangeContext(ctx context.Context, query *D.Msg) (*D.Msg, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type outcome struct {
		answer *D.Msg
		err    error
	}
	results := make(chan outcome, len(clients))
	for _, client := range clients {
		go func() {
			answer, err := client.ExchangeContext(ctx, query.Copy())
			if err == nil && (answer == nil || answer.Rcode == D.RcodeServerFailure || answer.Rcode == D.RcodeRefused) {
				err = errDoHResponse
			}
			results <- outcome{answer, err}
		}()
	}
	var failures []error
	for range clients {
		select {
		case result := <-results:
			if result.err == nil {
				return result.answer, nil
			}
			failures = append(failures, result.err)
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return nil, errors.Join(failures...)
}
