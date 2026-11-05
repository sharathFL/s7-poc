// Package model holds the protocol-independent data model.
package model

import (
	"encoding/json"
	"math"
	"time"
)

type Quality string

const (
	Good        Quality = "GOOD"
	Stale       Quality = "STALE"
	Invalid     Quality = "INVALID"
	CommFailure Quality = "COMM_FAILURE"
)

type Reading struct {
	DeviceID  string
	Protocol  string // s7comm | opcua | mock-s7comm | mock-opcua
	Tag       string
	Datatype  string
	Value     any
	Timestamp time.Time
	Quality   Quality
	Error     string
	LatencyMS *float64
	Metadata  map[string]any
}

const TimeFmt = "2006-01-02T15:04:05.000Z"

func FormatTime(t time.Time) string { return t.UTC().Format(TimeFmt) }

// MarshalJSON produces exactly the same JSON shape as the Python implementation.
func (r Reading) MarshalJSON() ([]byte, error) {
	var errp *string
	if r.Error != "" {
		e := r.Error
		errp = &e
	}
	var lat *float64
	if r.LatencyMS != nil {
		l := math.Round(*r.LatencyMS*100) / 100
		lat = &l
	}
	md := r.Metadata
	if md == nil {
		md = map[string]any{}
	}
	return json.Marshal(map[string]any{
		"device_id": r.DeviceID, "protocol": r.Protocol, "tag": r.Tag, "value": r.Value,
		"datatype": r.Datatype, "timestamp": FormatTime(r.Timestamp), "quality": r.Quality,
		"error": errp, "latency_ms": lat, "metadata": md,
	})
}

func Ptr[T any](v T) *T { return &v }
