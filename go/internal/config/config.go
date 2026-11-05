// Package config loads the shared YAML configuration. Secrets come from the environment only.
package config

import (
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

type Tag struct {
	Name      string `yaml:"name"`
	Type      string `yaml:"type"`
	S7Address string `yaml:"s7_address"`
	OpcuaNode string `yaml:"opcua_node"`
}

type S7 struct {
	Enabled   *bool `yaml:"enabled"`
	Rack      int   `yaml:"rack"`
	Slot      *int  `yaml:"slot"`
	Port      int   `yaml:"port"`
	PollingMS int   `yaml:"polling_ms"`
	TimeoutMS int   `yaml:"timeout_ms"`
}

type OpcUa struct {
	Enabled        *bool  `yaml:"enabled"`
	Endpoint       string `yaml:"endpoint"`
	PollingMS      int    `yaml:"polling_ms"`
	TimeoutMS      int    `yaml:"timeout_ms"`
	Mode           string `yaml:"mode"`
	SecurityMode   string `yaml:"security_mode"`
	SecurityPolicy string `yaml:"security_policy"`
	ClientCert     string `yaml:"client_cert"`
	ClientKey      string `yaml:"client_key"`
	ServerCert     string `yaml:"server_cert"`
	Username       string `yaml:"-"`
	Password       string `yaml:"-"`
}

type Device struct {
	ID    string `yaml:"id"`
	IP    string `yaml:"ip"`
	Mock  bool   `yaml:"mock"`
	S7    S7     `yaml:"s7comm"`
	OpcUa OpcUa  `yaml:"opcua"`
	Tags  []Tag  `yaml:"tags"`
}

type API struct {
	StaleAfterFactor float64 `yaml:"stale_after_factor"`
	HistorySize      int     `yaml:"history_size"`
}

type Config struct {
	API     API      `yaml:"api"`
	Devices []Device `yaml:"devices"`
}

func (s S7) On() bool    { return s.Enabled == nil || *s.Enabled }
func (o OpcUa) On() bool { return o.Enabled == nil || *o.Enabled }

func Parse(data []byte, forceMock bool) (*Config, error) {
	var c Config
	if err := yaml.Unmarshal(data, &c); err != nil {
		return nil, err
	}
	if len(c.Devices) == 0 {
		return nil, fmt.Errorf("no devices configured")
	}
	if c.API.StaleAfterFactor <= 0 {
		c.API.StaleAfterFactor = 3
	}
	if c.API.HistorySize <= 0 {
		c.API.HistorySize = 500
	}
	for i := range c.Devices {
		d := &c.Devices[i]
		if len(d.Tags) == 0 {
			return nil, fmt.Errorf("device %s: no tags configured", d.ID)
		}
		d.Mock = d.Mock || forceMock
		if d.S7.Slot == nil {
			d.S7.Slot = new(int)
			*d.S7.Slot = 1
		}
		def := func(p *int, v int) {
			if *p <= 0 {
				*p = v
			}
		}
		def(&d.S7.Port, 102)
		def(&d.S7.PollingMS, 1000)
		def(&d.S7.TimeoutMS, 3000)
		def(&d.OpcUa.PollingMS, 1000)
		def(&d.OpcUa.TimeoutMS, 4000)
		if d.OpcUa.Mode == "" {
			d.OpcUa.Mode = "poll"
		}
		if d.OpcUa.SecurityMode == "" {
			d.OpcUa.SecurityMode = "None"
		}
		if d.OpcUa.SecurityPolicy == "" {
			d.OpcUa.SecurityPolicy = "None"
		}
		d.OpcUa.Username, d.OpcUa.Password = envCreds(d.ID)
	}
	return &c, nil
}

func envCreds(id string) (string, string) {
	suffix := "_" + strings.ToUpper(strings.ReplaceAll(id, "-", "_"))
	get := func(k string) string {
		if v := os.Getenv(k + suffix); v != "" {
			return v
		}
		return os.Getenv(k)
	}
	pw := get("OPCUA_PASSWORD")
	if pw == "" {
		if f := os.Getenv("OPCUA_PASSWORD_FILE"); f != "" {
			if b, err := os.ReadFile(f); err == nil {
				pw = strings.TrimSpace(string(b))
			}
		}
	}
	return get("OPCUA_USERNAME"), pw
}

func Load() (*Config, error) {
	path := os.Getenv("CONFIG_PATH")
	if path == "" {
		path = "/config/devices.yaml"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	m := strings.ToLower(os.Getenv("S7POC_MOCK"))
	return Parse(b, m == "1" || m == "true" || m == "yes")
}
