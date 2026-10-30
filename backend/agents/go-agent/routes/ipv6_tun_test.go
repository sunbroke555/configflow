package routes

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func ipv6MissingTUN(f *ipv6Fixture) {
	f.links = strings.ReplaceAll(f.links, `"local":"fdfe::1"`, `"local":"fe80::9"`)
}

func TestIPv6ReadinessUsesActualTUNAddresses(t *testing.T) {
	f := newIPv6Fixture(t)
	f.ready(t)
	if r := inspectIPv6(f.cfg, ""); r.TUNIPv6.Status != "ready" || !r.ConfigurationReady {
		t.Fatalf("real TUN address not detected: %+v", r)
	}
	ipv6MissingTUN(f)
	r := inspectIPv6(f.cfg, "")
	if !r.TUNIPv6.Required || r.TUNIPv6.Status != "missing" || r.ConfigurationReady {
		t.Fatalf("YAML-only readiness overclaimed: %+v", r)
	}
	if f.writes != 0 {
		t.Fatal("read-only TUN status changed host")
	}
}

func TestIPv6TUNRecoveryReloadsOnlyOwnedRunningCoreOnce(t *testing.T) {
	for _, scenario := range []string{"recover", "already-ready", "unowned", "no-address", "no-route", "stopped", "bad-auth", "runtime-mismatch", "reload-fails", "reload-no-interface", "file-changed"} {
		t.Run(scenario, func(t *testing.T) {
			f := newIPv6Fixture(t)
			oldRunning, oldPause := ipv6CoreRunning, ipv6TUNPause
			t.Cleanup(func() { ipv6CoreRunning, ipv6TUNPause = oldRunning, oldPause })
			runningChecks := 0
			ipv6CoreRunning = func(*Config) (bool, error) { runningChecks++; return scenario != "stopped", nil }
			ipv6TUNPause = func(time.Duration) {}
			if scenario != "unowned" {
				if _, err := prepareIPv6(f.cfg, "eth1", 3); err != nil {
					t.Fatal(err)
				}
			} else {
				f.ready(t)
			}
			if scenario != "already-ready" {
				ipv6MissingTUN(f)
			}
			if scenario == "no-address" {
				f.links = strings.ReplaceAll(f.links, "2001:db8:150:1::2", "fdda::2")
			}
			if scenario == "no-route" {
				f.routes = "[]"
			}
			reloads := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer core-secret" {
					t.Error("controller authentication missing")
					w.WriteHeader(401)
					return
				}
				if scenario == "bad-auth" {
					w.WriteHeader(401)
					return
				}
				switch {
				case r.URL.Path == "/version":
					fmt.Fprint(w, `{"version":"test"}`)
				case r.URL.Path == "/configs" && r.Method == http.MethodGet:
					if scenario == "runtime-mismatch" {
						fmt.Fprint(w, `{"ipv6":false,"tun":{"enable":true,"device":"Meta"}}`)
					} else {
						fmt.Fprint(w, `{"ipv6":true,"tun":{"enable":true,"device":"Meta"}}`)
					}
					if scenario == "file-changed" {
						data, _ := os.ReadFile(f.cfg.ConfigPath)
						os.WriteFile(f.cfg.ConfigPath, append(data, []byte("# externally changed\n")...), 0600)
					}
				case r.URL.Path == "/configs" && r.Method == http.MethodPut:
					reloads++
					var body map[string]string
					if json.NewDecoder(r.Body).Decode(&body) != nil || body["path"] != f.cfg.ConfigPath || body["payload"] != "" || r.URL.Query().Get("force") != "true" {
						t.Error("uncontrolled reload request")
						w.WriteHeader(400)
						return
					}
					if scenario == "reload-fails" {
						w.WriteHeader(500)
						return
					}
					if scenario != "reload-no-interface" {
						f.links = strings.ReplaceAll(f.links, `"local":"fe80::9"`, `"local":"fdfe::1"`)
					}
					w.WriteHeader(204)
				default:
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			data, _ := os.ReadFile(f.cfg.ConfigPath)
			data = append(data, []byte("external-controller: "+strings.TrimPrefix(server.URL, "http://")+"\nsecret: core-secret\n")...)
			if err := os.WriteFile(f.cfg.ConfigPath, data, 0600); err != nil {
				t.Fatal(err)
			}
			network, err := detectIPv6Network()
			if err != nil {
				t.Fatal(err)
			}
			changed, err := recoverIPv6TUN(f.cfg, network)
			switch scenario {
			case "recover":
				if err != nil || !changed || reloads != 1 {
					t.Fatalf("did not recover: %v %v %d", changed, err, reloads)
				}
				network, _ = detectIPv6Network()
				changed, err = recoverIPv6TUN(f.cfg, network)
				if err != nil || changed || reloads != 1 {
					t.Fatalf("repeated recovery reloaded core: %v %v %d", changed, err, reloads)
				}
				after, _ := os.ReadFile(f.cfg.ConfigPath)
				if string(after) != string(data) {
					t.Fatal("recovery edited deployed configuration")
				}
			case "already-ready":
				if err != nil || changed || reloads != 0 || runningChecks != 0 {
					t.Fatal("ready core unnecessarily inspected or reloaded")
				}
			case "no-address", "no-route", "stopped":
				var waiting ipv6TUNWaiting
				if !errors.As(err, &waiting) || changed || reloads != 0 {
					t.Fatalf("should await readiness without touching IPv4: %v %v %d", changed, err, reloads)
				}
			case "reload-fails", "reload-no-interface":
				if err == nil || !changed || reloads != 1 {
					t.Fatal("reload failure hidden or retried", changed, err, reloads)
				}
			default:
				if err == nil || changed || reloads != 0 {
					t.Fatal("unsafe core reload", changed, err, reloads)
				}
			}
		})
	}
}

func TestIPv6ExplicitPrepareRecordsRecoveryWithoutOwningUserSysctl(t *testing.T) {
	f := newIPv6Fixture(t)
	f.ready(t)
	statePath, _ := ipv6StatePath(f.cfg)
	inspectIPv6(f.cfg, "")
	if _, err := os.Stat(statePath); !os.IsNotExist(err) {
		t.Fatal("GET authorized boot recovery")
	}
	userPath := ipv6SysctlDirectory + "/50-user.conf"
	before, _ := os.ReadFile(userPath)
	if changed, err := prepareIPv6(f.cfg, "eth1", 3); err != nil || changed {
		t.Fatal(changed, err)
	}
	state, err := loadIPv6Managed(f.cfg)
	if err != nil || state == nil || len(state.FileValues) != 0 || state.Phase != "active" {
		t.Fatal("explicit recovery authorization not recorded", state, err)
	}
	if _, err := os.Stat(ipv6ManagedPath()); !os.IsNotExist(err) {
		t.Fatal("took over complete user persistence")
	}
	after, _ := os.ReadFile(userPath)
	if string(before) != string(after) {
		t.Fatal("changed user file")
	}
	f.persist(t, "50-user.conf", "net.ipv6.conf.all.forwarding=0\n")
	writes := f.writes
	if _, err := prepareIPv6(f.cfg, "eth1", 3); err == nil || f.writes != writes {
		t.Fatal("authorization silently replaced changed user persistence", err)
	}
}

func TestIPv6TUNRequirementMatchesEffectiveMihomoDNS(t *testing.T) {
	for _, scenario := range []string{"core-default-true", "core-explicit-false", "dns-disabled", "redir-host"} {
		t.Run(scenario, func(t *testing.T) {
			f := newIPv6Fixture(t)
			f.ready(t)
			ipv6MissingTUN(f)
			data, _ := os.ReadFile(f.cfg.ConfigPath)
			config := string(data)
			switch scenario {
			case "core-default-true":
				config = strings.Replace(config, "ipv6: true\n", "", 1)
			case "core-explicit-false":
				config = strings.Replace(config, "ipv6: true\n", "ipv6: false\n", 1)
			case "dns-disabled":
				config = strings.Replace(config, "  enable: true\n", "  enable: false\n", 1)
			case "redir-host":
				config = strings.Replace(config, "enhanced-mode: fake-ip", "enhanced-mode: redir-host", 1)
			}
			if err := os.WriteFile(f.cfg.ConfigPath, []byte(config), 0600); err != nil {
				t.Fatal(err)
			}
			network, _ := detectIPv6Network()
			status := ipv6TUNStatus(f.cfg, network)
			if scenario == "core-default-true" {
				if !status.Required || status.Status != "missing" {
					t.Fatal("ignored effective default IPv6", status)
				}
			} else if status.Required || status.Status != "not_required" {
				t.Fatal("inactive Fake-IP DNS counted required", status)
			}
		})
	}
}
