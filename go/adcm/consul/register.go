// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package consul implements service registration and TTL-based health
// checking with a Consul agent.  The status service registers itself on
// startup so that Celery workers can discover it via Consul's health API.
package consul

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds Consul registration parameters read from env.
type Config struct {
	ConsulURL      string
	Datacenter     string
	ACLToken       string
	CACertFile     string
	ClientCertFile string
	ClientKeyFile  string

	ServiceName string
	ServiceID   string
	ServicePort int
	HealthTTL   time.Duration
}

// ConfigFromEnv builds a Config from environment variables.
// Returns nil if CONSUL_URL is not set (Consul registration disabled).
func ConfigFromEnv(servicePort int) *Config {
	consulURL := os.Getenv("CONSUL_URL")
	if consulURL == "" {
		return nil
	}

	ttlStr := os.Getenv("CONSUL_HEALTH_CHECK_TTL")
	ttl := 30.0
	if ttlStr != "" {
		if parsed, err := strconv.ParseFloat(ttlStr, 64); err == nil {
			ttl = parsed
		}
	}

	serviceName := os.Getenv("STATUS_SERVICE_NAME")
	if serviceName == "" {
		serviceName = "adcm-status-service"
	}

	serviceID := os.Getenv("STATUS_SERVICE_ID")
	if serviceID == "" {
		serviceID = "adcm-status-service"
	}

	return &Config{
		ConsulURL:      strings.TrimRight(consulURL, "/"),
		Datacenter:     os.Getenv("CONSUL_DATACENTER"),
		ACLToken:       os.Getenv("CONSUL_ACL_TOKEN"),
		CACertFile:     os.Getenv("CONSUL_CACERT_FILE"),
		ClientCertFile: os.Getenv("CONSUL_CLIENT_CERT_FILE"),
		ClientKeyFile:  os.Getenv("CONSUL_CLIENT_KEY_FILE"),
		ServiceName:    serviceName,
		ServiceID:      serviceID,
		ServicePort:    servicePort,
		HealthTTL:      time.Duration(ttl * float64(time.Second)),
	}
}

// serviceRegistration is the JSON body for PUT /v1/agent/service/register.
type serviceRegistration struct {
	ID      string            `json:"ID"`
	Name    string            `json:"Name"`
	Address string            `json:"Address"`
	Port    int               `json:"Port"`
	Meta    map[string]string `json:"Meta,omitempty"`
	Check   checkDefinition   `json:"Check"`
}

type checkDefinition struct {
	CheckID                        string `json:"CheckID"`
	TTL                            string `json:"TTL"`
	DeregisterCriticalServiceAfter string `json:"DeregisterCriticalServiceAfter"`
}

// Registrar handles maintaining a registered service in Consul.
type Registrar struct {
	cfg    *Config
	client *http.Client
	stopCh chan struct{}
}

// Register creates a Registrar, registers the service with Consul, and starts
// a background goroutine that periodically passes the TTL health check.
func Register(cfg *Config) (*Registrar, error) {
	transport, err := buildTransport(cfg)
	if err != nil {
		return nil, fmt.Errorf("build consul HTTP transport: %w", err)
	}

	r := &Registrar{
		cfg: cfg,
		client: &http.Client{
			Transport: transport,
			Timeout:   10 * time.Second,
		},
		stopCh: make(chan struct{}),
	}

	address := getEnvOrDefault("STATUS_SERVICE_ADDRESS", discoverOwnAddress())

	reg := serviceRegistration{
		ID:      cfg.ServiceID,
		Name:    cfg.ServiceName,
		Address: address,
		Port:    cfg.ServicePort,
		Meta: map[string]string{
			"scheme":    "http",
			"base_path": "/api/v1/",
		},
		Check: checkDefinition{
			CheckID:                        fmt.Sprintf("service:%s:ttl", cfg.ServiceID),
			TTL:                            cfg.HealthTTL.String(),
			DeregisterCriticalServiceAfter: (3 * cfg.HealthTTL).String(),
		},
	}

	body, err := json.Marshal(reg)
	if err != nil {
		return nil, fmt.Errorf("marshal registration body: %w", err)
	}

	url := fmt.Sprintf("%s/v1/agent/service/register", cfg.ConsulURL)
	req, err := http.NewRequest(http.MethodPut, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create registration request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	r.setAuth(req)

	resp, err := r.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("consul register request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("consul register failed: status %d", resp.StatusCode)
	}

	log.Printf("[consul] Registered service %s (id=%s) at %s:%d", cfg.ServiceName, cfg.ServiceID, address, cfg.ServicePort)

	// Pass the initial TTL check immediately
	if err := r.passTTL(); err != nil {
		log.Printf("[consul] Warning: initial TTL pass failed: %v", err)
	}

	// Start background TTL updater
	go r.ttlLoop()

	return r, nil
}

// Deregister removes the service from Consul and stops the TTL goroutine.
func (r *Registrar) Deregister() {
	close(r.stopCh)

	url := fmt.Sprintf("%s/v1/agent/service/deregister/%s", r.cfg.ConsulURL, r.cfg.ServiceID)
	req, err := http.NewRequest(http.MethodPut, url, nil)
	if err != nil {
		log.Printf("[consul] Failed to build deregister request: %v", err)
		return
	}
	r.setAuth(req)

	resp, err := r.client.Do(req)
	if err != nil {
		log.Printf("[consul] Failed to deregister: %v", err)
		return
	}
	resp.Body.Close()
	log.Printf("[consul] Deregistered service %s", r.cfg.ServiceID)
}

func (r *Registrar) ttlLoop() {
	// Pass TTL at half the interval to avoid expiry.
	interval := r.cfg.HealthTTL / 2
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			if err := r.passTTL(); err != nil {
				log.Printf("[consul] TTL pass failed: %v", err)
			}
		case <-r.stopCh:
			return
		}
	}
}

func (r *Registrar) passTTL() error {
	checkID := fmt.Sprintf("service:%s:ttl", r.cfg.ServiceID)
	url := fmt.Sprintf("%s/v1/agent/check/pass/%s", r.cfg.ConsulURL, checkID)

	req, err := http.NewRequest(http.MethodPut, url, nil)
	if err != nil {
		return err
	}
	r.setAuth(req)

	resp, err := r.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("TTL pass returned status %d", resp.StatusCode)
	}
	return nil
}

func (r *Registrar) setAuth(req *http.Request) {
	if r.cfg.ACLToken != "" {
		req.Header.Set("X-Consul-Token", r.cfg.ACLToken)
	}
}

func buildTransport(cfg *Config) (*http.Transport, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()

	if cfg.CACertFile == "" && cfg.ClientCertFile == "" {
		return transport, nil
	}

	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS12,
	}

	if cfg.CACertFile != "" {
		caCert, err := os.ReadFile(cfg.CACertFile)
		if err != nil {
			return nil, fmt.Errorf("read CA cert %s: %w", cfg.CACertFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caCert) {
			return nil, fmt.Errorf("failed to parse CA cert from %s", cfg.CACertFile)
		}
		tlsConfig.RootCAs = pool
	}

	if cfg.ClientCertFile != "" && cfg.ClientKeyFile != "" {
		cert, err := tls.LoadX509KeyPair(cfg.ClientCertFile, cfg.ClientKeyFile)
		if err != nil {
			return nil, fmt.Errorf("load client cert/key: %w", err)
		}
		tlsConfig.Certificates = []tls.Certificate{cert}
	}

	transport.TLSClientConfig = tlsConfig
	return transport, nil
}

// getEnvOrDefault returns the value of the environment variable named by key,
// or fallback if the variable is unset or empty.
func getEnvOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// discoverOwnAddress returns the address to register in Consul.
//
// Priority:
//  1. DEFAULT_ADCM_URL env — extract host from the URL.
//  2. UDP dial to 8.8.8.8 — picks the preferred outbound IP.
//  3. os.Hostname() as last resort.
func discoverOwnAddress() string {
	if rawURL := os.Getenv("DEFAULT_ADCM_URL"); rawURL != "" {
		if parsed, err := url.Parse(rawURL); err == nil && parsed.Hostname() != "" {
			return parsed.Hostname()
		}
	}

	conn, err := net.Dial("udp", "8.8.8.8:80")
	if err != nil {
		hostname, _ := os.Hostname()
		return hostname
	}
	defer conn.Close()

	localAddr := conn.LocalAddr().(*net.UDPAddr)
	return localAddr.IP.String()
}
