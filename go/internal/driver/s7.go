package driver

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	plc4go "github.com/apache/plc4x/plc4go/pkg/api"
	"github.com/apache/plc4x/plc4go/pkg/api/drivers"
	apiModel "github.com/apache/plc4x/plc4go/pkg/api/model"
	"github.com/apache/plc4x/plc4go/pkg/api/transports"
	"github.com/apache/plc4x/plc4go/pkg/api/values"

	"s7poc/internal/config"
	"s7poc/internal/model"
)

// S7 reads via the Apache PLC4X Go S7 driver. READ-ONLY: only ReadRequestBuilder is ever used.
type S7 struct {
	dev  config.Device
	mu   sync.Mutex
	dm   plc4go.PlcDriverManager
	conn plc4go.PlcConnection
}

func NewS7(d config.Device) *S7 { return &S7{dev: d} }

func (s *S7) Protocol() string { return "s7comm" }
func (s *S7) Kind() string     { return "s7comm" }
func (s *S7) PollingMS() int   { return s.dev.S7.PollingMS }
func (s *S7) Connected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.conn != nil && s.conn.IsConnected()
}

func (s *S7) url() string {
	c := s.dev.S7
	return fmt.Sprintf("s7://%s:%d?cotp.remote-rack=%d&cotp.remote-slot=%d&read-timeout-ms=%d&cotp.connect-timeout-ms=%d&cotp.handshake-timeout-ms=%d",
		s.dev.IP, c.Port, c.Rack, *c.Slot, c.TimeoutMS, c.TimeoutMS, c.TimeoutMS)
}

// ToPlc4xAddress converts "DB10.DBD0"/"DB10.DBX12.0" + type into PLC4X syntax "%DB10.DBD0:REAL".
// Fixed-length strings are written "STRING[20]" in the config. plc4go's "[a..b]:STRING" means an
// ARRAY of strings, so we read the S7 string as raw bytes (max_len, act_len, chars = n+2 bytes)
// and decode the header ourselves.
var sizedType = regexp.MustCompile(`^STRING\[(\d+)\]$`)

func ToPlc4xAddress(addr, typ string) string {
	a := strings.TrimSpace(addr)
	if !strings.HasPrefix(a, "%") {
		a = "%" + a
	}
	if !strings.Contains(a, ":") {
		t := strings.ToUpper(strings.TrimSpace(typ))
		if m := sizedType.FindStringSubmatch(t); m != nil {
			if n, err := strconv.Atoi(m[1]); err == nil && n > 0 && n <= 254 {
				return fmt.Sprintf("%s[0..%d]:BYTE", a, n+1)
			}
		}
		a += ":" + t
	}
	return a
}

func (s *S7) Connected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.conn != nil && s.conn.IsConnected()
}

func (s *S7) url() string {
	c := s.dev.S7
	return fmt.Sprintf("s7://%s:%d?cotp.remote-rack=%d&cotp.remote-slot=%d&read-timeout-ms=%d&cotp.connect-timeout-ms=%d&cotp.handshake-timeout-ms=%d",
		s.dev.IP, c.Port, c.Rack, *c.Slot, c.TimeoutMS, c.TimeoutMS, c.TimeoutMS)
}

// ToPlc4xAddress converts "DB10.DBD0"/"DB10.DBX12.0" + type into PLC4X syntax "%DB10.DBD0:REAL".
// Fixed-length strings are written "STRING[20]" in the config; plc4go now wants the length as a
// range before the type: "%DB7.DBB6[0..19]:STRING".
var sizedType = regexp.MustCompile(`^(W?STRING)\[(\d+)\]$`)

func ToPlc4xAddress(addr, typ string) string {
	a := strings.TrimSpace(addr)
	if !strings.HasPrefix(a, "%") {
		a = "%" + a
	}
	if !strings.Contains(a, ":") {
		t := strings.ToUpper(strings.TrimSpace(typ))
		if m := sizedType.FindStringSubmatch(t); m != nil {
			if n, err := strconv.Atoi(m[2]); err == nil && n > 0 {
				return fmt.Sprintf("%s[0..%d]:%s", a, n-1, m[1])
			}
		}
		a += ":" + t
	}
	return a
}

func (s *S7) Connect(ctx context.Context) error {
	s.Close()
	dm := plc4go.NewPlcDriverManager()
	transports.RegisterTcpTransport(dm)
	drivers.RegisterS7Driver(dm)
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.dev.S7.TimeoutMS*3)*time.Millisecond)
	defer cancel()
	conn, err := dm.GetConnection(ctx, s.url())
	if err != nil {
		_ = dm.Close()
		return fmt.Errorf("%w: S7 connect to %s rack=%d slot=%d: %v", ErrComm, s.dev.IP, s.dev.S7.Rack, *s.dev.S7.Slot, err)
	}
	s.mu.Lock()
	s.dm, s.conn = dm, conn
	s.mu.Unlock()
	return nil
}

func (s *S7) Close() {
	s.mu.Lock()
	c, dm := s.conn, s.dm
	s.conn, s.dm = nil, nil
	s.mu.Unlock()
	if c != nil {
		c.Close()
	}
	if dm != nil {
		_ = dm.Close()
	}
}

// convertTyped picks the Go type from the CONFIGURED S7 type. plc4go's IsBool()/IsFloat32() answer
// true for many numeric values (they are convertible), so probing Is*() in order gives wrong types.
func convertTyped(typ string, v values.PlcValue) any {
	switch t := strings.ToUpper(typ); t {
	case "BOOL":
		return v.GetBool()
	case "REAL":
		return v.GetFloat32()
	case "LREAL":
		return v.GetFloat64()
	case "SINT":
		return v.GetInt8()
	case "INT":
		return v.GetInt16()
	case "DINT", "TIME":
		return v.GetInt32()
	case "LINT":
		return v.GetInt64()
	case "USINT", "BYTE":
		return v.GetUint8()
	case "UINT", "WORD":
		return v.GetUint16()
	case "UDINT", "DWORD":
		return v.GetUint32()
	case "ULINT", "LWORD":
		return v.GetUint64()
	default:
		if sizedType.MatchString(t) && v.IsList() {
			items := v.GetList()
			if len(items) >= 2 {
				b := make([]byte, len(items))
				for i, it := range items {
					b[i] = it.GetUint8()
				}
				n := int(b[1])
				if m := int(b[0]); n > m {
					n = m
				}
				if n > len(b)-2 {
					n = len(b) - 2
				}
				return string(b[2 : 2+n])
			}
		}
		return convert(v)
	}
}

func convert(v values.PlcValue) any {
	switch {
	case v.IsBool():
		return v.GetBool()
	case v.IsFloat32():
		return v.GetFloat32()
	case v.IsFloat64():
		return v.GetFloat64()
	case v.IsInt8():
		return v.GetInt8()
	case v.IsInt16():
		return v.GetInt16()
	case v.IsInt32():
		return v.GetInt32()
	case v.IsInt64():
		return v.GetInt64()
	case v.IsUint8():
		return v.GetUint8()
	case v.IsUint16():
		return v.GetUint16()
	case v.IsUint32():
		return v.GetUint32()
	case v.IsUint64():
		return v.GetUint64()
	case v.IsString():
		return v.GetString()
	}
	return v.GetString()
}

func (s *S7) Read(ctx context.Context) ([]model.Reading, error) {
	s.mu.Lock()
	c := s.conn
	s.mu.Unlock()
	if c == nil || !c.IsConnected() {
		return nil, fmt.Errorf("%w: S7 not connected", ErrComm)
	}
	rb := c.ReadRequestBuilder()
	types := map[string]string{}
	n := 0
	for _, t := range s.dev.Tags {
		if t.S7Address == "" {
			continue
		}
		rb.AddTagAddress(t.Name, ToPlc4xAddress(t.S7Address, t.Type))
		types[t.Name] = strings.ToUpper(t.Type)
		n++
	}
	if n == 0 {
		return nil, nil
	}
	req, err := rb.Build()
	if err != nil {
		return nil, fmt.Errorf("invalid S7 tag configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.dev.S7.TimeoutMS)*time.Millisecond)
	defer cancel()
	t0 := time.Now()
	var res apiModel.PlcReadRequestResult
	select {
	case res = <-req.Execute(ctx):
	case <-ctx.Done():
		return nil, fmt.Errorf("%w: S7 read timeout", ErrComm)
	}
	lat := float64(time.Since(t0).Microseconds()) / 1000
	if res.GetErr() != nil {
		return nil, fmt.Errorf("%w: S7 read failed: %v", ErrComm, res.GetErr())
	}
	resp := res.GetResponse()
	now := time.Now()
	var out []model.Reading
	for _, t := range s.dev.Tags {
		if t.S7Address == "" {
			continue
		}
		md := map[string]any{"address": t.S7Address}
		r := model.Reading{DeviceID: s.dev.ID, Protocol: "s7comm", Tag: t.Name, Datatype: types[t.Name],
			Timestamp: now, LatencyMS: &lat, Metadata: md}
		if code := resp.GetResponseCode(t.Name); code != apiModel.PlcResponseCode_OK {
			r.Quality, r.Error = model.Invalid, "response code "+code.String()
		} else {
			r.Quality, r.Value = model.Good, convertTyped(types[t.Name], resp.GetValue(t.Name))
		}
		out = append(out, r)
	}
	return out, nil
}

func (s *S7) Browse(context.Context, string, int) ([]map[string]any, error) {
	return nil, fmt.Errorf("s7comm does not support browsing in this PoC")
}
