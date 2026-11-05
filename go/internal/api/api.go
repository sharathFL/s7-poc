// Package api exposes the REST API. READ-ONLY: only GET routes are registered (other methods => 405).
package api

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"s7poc/internal/acq"
	"s7poc/internal/compare"
	"s7poc/internal/model"
)

type Server struct {
	A       *acq.Acquisition
	Dash    string
	Version string
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func httpErr(w http.ResponseWriter, code int, msg string) { writeJSON(w, code, map[string]string{"detail": msg}) }

func (s *Server) dev(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("id")
	if _, ok := s.A.Devices[id]; !ok {
		httpErr(w, 404, "unknown device \""+id+"\"")
		return "", false
	}
	return id, true
}

func kind(w http.ResponseWriter, r *http.Request) (string, bool) {
	p := r.URL.Query().Get("protocol")
	if p != "" && p != "s7comm" && p != "opcua" {
		httpErr(w, 422, "protocol must be s7comm or opcua")
		return "", false
	}
	return p, true
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

func (s *Server) Handler() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		conn := 0
		for _, c := range s.A.Channels {
			if c.Drv.Connected() {
				conn++
			}
		}
		writeJSON(w, 200, map[string]any{"status": "ok", "time": model.FormatTime(time.Now()), "implementation": "go",
			"devices": len(s.A.Devices), "channels_connected": conn, "channels_total": len(s.A.Channels)})
	})
	m.HandleFunc("GET /devices", func(w http.ResponseWriter, r *http.Request) {
		out := []map[string]any{}
		for _, id := range s.A.Order {
			d := s.A.Devices[id]
			out = append(out, map[string]any{"id": d.ID, "ip": d.IP, "mock": d.Mock, "tags": len(d.Tags),
				"state": s.A.DeviceStatus(id)["state"]})
		}
		writeJSON(w, 200, out)
	})
	m.HandleFunc("GET /devices/{id}/status", func(w http.ResponseWriter, r *http.Request) {
		if id, ok := s.dev(w, r); ok {
			writeJSON(w, 200, s.A.DeviceStatus(id))
		}
	})
	m.HandleFunc("GET /devices/{id}/tags", func(w http.ResponseWriter, r *http.Request) {
		id, ok := s.dev(w, r)
		if !ok {
			return
		}
		out := []map[string]any{}
		for _, t := range s.A.Devices[id].Tags {
			out = append(out, map[string]any{"name": t.Name, "type": t.Type, "s7_address": nilIfEmpty(t.S7Address), "opcua_node": nilIfEmpty(t.OpcuaNode)})
		}
		writeJSON(w, 200, out)
	})
	m.HandleFunc("GET /devices/{id}/readings", func(w http.ResponseWriter, r *http.Request) {
		id, ok := s.dev(w, r)
		if !ok {
			return
		}
		k, ok := kind(w, r)
		if !ok {
			return
		}
		q := r.URL.Query()
		limit, err := strconv.Atoi(q.Get("limit"))
		if err != nil {
			limit = 100
		}
		if limit < 1 || limit > 5000 {
			httpErr(w, 422, "limit must be 1..5000")
			return
		}
		if h := q.Get("history"); h == "true" || h == "1" {
			writeJSON(w, 200, nonNil(s.A.History(id, q.Get("tag"), k, limit)))
			return
		}
		writeJSON(w, 200, nonNil(s.A.Latest(id, q.Get("tag"), k)))
	})
	m.HandleFunc("GET /devices/{id}/tags/{tag}/latest", func(w http.ResponseWriter, r *http.Request) {
		id, ok := s.dev(w, r)
		if !ok {
			return
		}
		k, ok := kind(w, r)
		if !ok {
			return
		}
		tag := r.PathValue("tag")
		found := false
		for _, t := range s.A.Devices[id].Tags {
			found = found || t.Name == tag
		}
		if !found {
			httpErr(w, 404, "unknown tag \""+tag+"\"")
			return
		}
		rs := s.A.Latest(id, tag, k)
		if len(rs) == 0 {
			httpErr(w, 404, "no reading yet")
			return
		}
		writeJSON(w, 200, rs)
	})
	m.HandleFunc("GET /opcua/browse", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		id := q.Get("device_id")
		if id == "" && len(s.A.Order) > 0 {
			id = s.A.Order[0]
		}
		if _, ok := s.A.Devices[id]; !ok {
			httpErr(w, 404, "unknown device")
			return
		}
		ch := s.A.Channels[[2]string{id, "opcua"}]
		if ch == nil {
			httpErr(w, 404, "OPC UA not enabled for this device")
			return
		}
		if !ch.Drv.Connected() {
			httpErr(w, 503, "OPC UA not connected")
			return
		}
		depth, err := strconv.Atoi(q.Get("depth"))
		if err != nil {
			depth = 2
		}
		ctx, cancel := context.WithTimeout(r.Context(), 35*time.Second)
		defer cancel()
		nodes, err := ch.Drv.Browse(ctx, q.Get("start"), depth)
		if err != nil {
			httpErr(w, 502, "browse failed: "+err.Error())
			return
		}
		start := q.Get("start")
		if start == "" {
			start = "Objects"
		}
		writeJSON(w, 200, map[string]any{"device_id": id, "start": start, "count": len(nodes), "nodes": nonNil(nodes)})
	})
	m.HandleFunc("GET /comparison", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		rel, abs := 0.02, 0.05
		if v, err := strconv.ParseFloat(q.Get("rel_tol"), 64); err == nil && v >= 0 {
			rel = v
		}
		if v, err := strconv.ParseFloat(q.Get("abs_tol"), 64); err == nil && v >= 0 {
			abs = v
		}
		if id := q.Get("device_id"); id != "" {
			if _, ok := s.A.Devices[id]; !ok {
				httpErr(w, 404, "unknown device")
				return
			}
			writeJSON(w, 200, compare.Device(s.A, id, rel, abs))
			return
		}
		out := []map[string]any{}
		for _, id := range s.A.Order {
			out = append(out, compare.Device(s.A, id, rel, abs))
		}
		writeJSON(w, 200, out)
	})
	if st, err := os.Stat(s.Dash); err == nil && st.IsDir() {
		m.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.Dir(s.Dash))))
		m.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
			http.ServeFile(w, r, filepath.Join(s.Dash, "index.html"))
		})
	}
	return m
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
