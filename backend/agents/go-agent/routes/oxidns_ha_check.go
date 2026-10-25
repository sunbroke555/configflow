package routes

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"time"
)

const haCheckName = "configflow-ha-check.invalid."
const haCheckTimeout = time.Second

// haCheckTarget picks the loopback address of the wildcard UDP listener that
// serves the virtual IP. A missing or unreadable configuration falls back to
// port 53. The reserved watchdog name is preferred when the entry answers it
// locally, so upstream latency never looks like a failed local process.
func haCheckTarget(configPath string) (string, string) {
	address, name := "127.0.0.1:53", haCheckName
	if configPath == "" {
		return address, name
	}
	doc, err := readOxiDNSDocument(configPath)
	if err != nil {
		return address, name
	}
	var chosen *haListener
	listeners := haListeners(doc)
	for i := range listeners {
		if listeners[i].Wildcard && (chosen == nil || (listeners[i].Port == "53" && chosen.Port != "53")) {
			chosen = &listeners[i]
		}
	}
	if chosen == nil {
		return address, name
	}
	address = net.JoinHostPort("127.0.0.1", chosen.Port)
	plugins, _ := doc["plugins"].([]interface{})
	for _, value := range plugins {
		plugin, _ := value.(map[string]interface{})
		if tag, _ := plugin["tag"].(string); tag == "" || tag != chosen.Entry || plugin["type"] != "sequence" {
			continue
		}
		steps, _ := plugin["args"].([]interface{})
		if len(steps) > 0 && isWatchdogLocalRule(steps[0]) {
			name = watchdogProbeName
		}
		break
	}
	return address, name
}

// haDNSAnswers accepts any reply with the same ID and QR set: an NXDOMAIN or
// SERVFAIL still proves that the local process is answering.
func haDNSAnswers(address, name string, timeout time.Duration) error {
	conn, err := net.DialTimeout("udp", address, timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	var idBytes [2]byte
	if _, err = rand.Read(idBytes[:]); err != nil {
		return err
	}
	query := buildDNSQuery(binary.BigEndian.Uint16(idBytes[:]), name)
	if err = conn.SetDeadline(time.Now().Add(timeout)); err != nil {
		return err
	}
	if _, err = conn.Write(query); err != nil {
		return err
	}
	reply := make([]byte, 1500)
	for {
		n, err := conn.Read(reply)
		if err != nil {
			return fmt.Errorf("no reply: %w", err)
		}
		if n >= 12 && reply[0] == query[0] && reply[1] == query[1] && reply[2]&0x80 != 0 {
			return nil
		}
	}
}

func haCheckLocal(cfg *Config) error {
	configPath := ""
	if cfg != nil {
		configPath = cfg.ConfigPath
	}
	address, name := haCheckTarget(configPath)
	return haDNSAnswers(address, name, haCheckTimeout)
}

// RunOxiDNSHACheck is keepalived's track_script. It only reads the Agent and
// OxiDNS configuration and sends one local query: no registration, listener,
// file write or log output. Exit 0 means the local OxiDNS process answers.
func RunOxiDNSHACheck(agentConfigFile string) int {
	configPath := ""
	if data, err := os.ReadFile(agentConfigFile); err == nil {
		var agent struct {
			ConfigPath string `json:"config_path"`
		}
		if json.Unmarshal(data, &agent) == nil {
			configPath = agent.ConfigPath
		}
	}
	address, name := haCheckTarget(configPath)
	if haDNSAnswers(address, name, haCheckTimeout) != nil {
		return 1
	}
	return 0
}
