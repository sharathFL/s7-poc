// Package compare compares S7comm and OPC UA readings. Reads are independent samples, so values
// are compared with a tolerance and the sampling skew is reported, never assumed to be zero.
package compare

import (
	"math"
	"time"

	"s7poc/internal/acq"
	"s7poc/internal/model"
)

var equiv = map[string]string{
	"REAL": "Float", "LREAL": "Double", "BOOL": "Boolean", "INT": "Int16", "UINT": "UInt16", "WORD": "UInt16",
	"DINT": "Int32", "UDINT": "UInt32", "DWORD": "UInt32", "BYTE": "Byte", "SINT": "SByte", "USINT": "Byte",
	"STRING": "String", "LINT": "Int64", "LWORD": "UInt64", "ULINT": "UInt64",
}

func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float32:
		return float64(x), true
	case float64:
		return x, true
	case int:
		return float64(x), true
	case int8:
		return float64(x), true
	case int16:
		return float64(x), true
	case int32:
		return float64(x), true
	case int64:
		return float64(x), true
	case uint8:
		return float64(x), true
	case uint16:
		return float64(x), true
	case uint32:
		return float64(x), true
	case uint64:
		return float64(x), true
	}
	return 0, false
}

func isClose(a, b, rel, abs float64) bool {
	return math.Abs(a-b) <= math.Max(rel*math.Max(math.Abs(a), math.Abs(b)), abs)
}

func ValuesMatch(a, b any, rel, abs float64) bool {
	ab, aIsB := a.(bool)
	bb, bIsB := b.(bool)
	if aIsB || bIsB {
		return aIsB && bIsB && ab == bb
	}
	fa, ok1 := toFloat(a)
	fb, ok2 := toFloat(b)
	if ok1 && ok2 {
		return isClose(fa, fb, rel, abs)
	}
	return a == b
}

func ageMS(r model.Reading) float64 {
	return math.Round(float64(time.Since(r.Timestamp).Microseconds())/100) / 10
}

func Tag(name string, s7, ua *model.Reading, rel, abs float64) map[string]any {
	res := map[string]any{"tag": name, "s7comm": nil, "opcua": nil}
	if s7 != nil {
		res["s7comm"] = *s7
	}
	if ua != nil {
		res["opcua"] = *ua
	}
	if s7 == nil || ua == nil {
		res["status"], res["match"], res["value_diff"], res["skew_ms"], res["type_match"] = "INCOMPLETE", nil, nil, nil, nil
		return res
	}
	usable := s7.Quality == model.Good && ua.Quality == model.Good
	skew := math.Abs(float64(s7.Timestamp.Sub(ua.Timestamp).Microseconds())) / 1000
	want, known := equiv[s7.Datatype]
	typeMatch := !known || want == ua.Datatype
	res["skew_ms"] = math.Round(skew*10) / 10
	res["type_match"] = typeMatch
	res["freshness_ms"] = map[string]any{"s7comm": ageMS(*s7), "opcua": ageMS(*ua)}
	res["value_diff"], res["match"], res["status"] = nil, nil, "UNUSABLE"
	if usable {
		m := ValuesMatch(s7.Value, ua.Value, rel, abs)
		res["match"] = m
		res["status"] = map[bool]string{true: "MATCH", false: "DIFFERENT"}[m]
		_, isB := s7.Value.(bool)
		fa, ok1 := toFloat(s7.Value)
		fb, ok2 := toFloat(ua.Value)
		if ok1 && ok2 && !isB {
			res["value_diff"] = fb - fa
		}
	}
	return res
}

func Device(a *acq.Acquisition, id string, rel, abs float64) map[string]any {
	var tags []map[string]any
	compared, matching, typeMis := 0, 0, 0
	maxSkew := any(nil)
	for _, t := range a.Devices[id].Tags {
		var s, u *model.Reading
		if l := a.Latest(id, t.Name, "s7comm"); len(l) > 0 {
			s = &l[0]
		}
		if l := a.Latest(id, t.Name, "opcua"); len(l) > 0 {
			u = &l[0]
		}
		r := Tag(t.Name, s, u, rel, abs)
		tags = append(tags, r)
		if m, ok := r["match"].(bool); ok {
			compared++
			if m {
				matching++
			}
		}
		if tm, ok := r["type_match"].(bool); ok && !tm {
			typeMis++
		}
		if sk, ok := r["skew_ms"].(float64); ok {
			if cur, ok := maxSkew.(float64); !ok || sk > cur {
				maxSkew = sk
			}
		}
	}
	stats := map[string]any{}
	for k, ch := range a.Channels {
		if k[0] != id {
			continue
		}
		st := a.ChannelStatus(ch)
		stats[k[1]] = map[string]any{"success_rate": st["success_rate"], "latency_ms": st["latency_ms"],
			"reads_ok": st["reads_ok"], "reads_fail": st["reads_fail"], "connected": st["connected"]}
	}
	return map[string]any{
		"device_id": id, "tolerance": map[string]any{"relative": rel, "absolute": abs},
		"note": "Independent samples: values may legitimately differ by process change between reads (see skew_ms).",
		"tags": tags,
		"summary": map[string]any{"compared": compared, "matching": matching, "type_mismatches": typeMis, "max_skew_ms": maxSkew},
		"protocol_stats": stats,
	}
}
