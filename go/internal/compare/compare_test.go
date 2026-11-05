package compare

import (
	"testing"
	"time"

	"s7poc/internal/model"
)

func rd(dt string, v any, q model.Quality, off time.Duration) *model.Reading {
	return &model.Reading{Tag: "T", Datatype: dt, Value: v, Quality: q, Timestamp: time.Now().Add(off)}
}

func TestMatchWithinTolerance(t *testing.T) {
	r := Tag("T", rd("REAL", float32(35.5), model.Good, 0), rd("Float", float32(35.52), model.Good, 200*time.Millisecond), 0.02, 0.05)
	if r["status"] != "MATCH" || r["type_match"] != true {
		t.Fatalf("%v", r)
	}
	if s := r["skew_ms"].(float64); s < 150 || s > 250 {
		t.Fatalf("skew %v", s)
	}
}

func TestDifferentTypeUnusableIncomplete(t *testing.T) {
	if Tag("T", rd("REAL", 35.0, model.Good, 0), rd("Float", 50.0, model.Good, 0), 0.02, 0.05)["status"] != "DIFFERENT" {
		t.Fatal("expected DIFFERENT")
	}
	if Tag("T", rd("DINT", int32(1), model.Good, 0), rd("Int16", int16(1), model.Good, 0), 0.02, 0.05)["type_match"] != false {
		t.Fatal("expected type mismatch")
	}
	if Tag("T", rd("REAL", 1.0, model.CommFailure, 0), rd("Float", 1.0, model.Good, 0), 0.02, 0.05)["status"] != "UNUSABLE" {
		t.Fatal("expected UNUSABLE")
	}
	if Tag("T", rd("REAL", 1.0, model.Good, 0), nil, 0.02, 0.05)["status"] != "INCOMPLETE" {
		t.Fatal("expected INCOMPLETE")
	}
}

func TestBool(t *testing.T) {
	if !ValuesMatch(true, true, 0.02, 0.05) || ValuesMatch(true, false, 0.02, 0.05) || ValuesMatch(true, 1, 0.02, 0.05) {
		t.Fatal("bool compare wrong")
	}
}
