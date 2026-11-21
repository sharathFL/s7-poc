package driver

import (
	"math"
	"testing"

	"s7poc/internal/config"
)

func TestParseS7Addr(t *testing.T) {
	ok := map[string]s7Loc{
		"DB7.DBW0":      {7, 0, 0},
		"%DB10.DBD14":   {10, 14, 0},
		"DB7.DBX28.0":   {7, 28, 0},
		"db7.dbx28.5":   {7, 28, 5},
		"DB10.DBW2:INT": {10, 2, 0},
	}
	for in, want := range ok {
		got, err := parseS7Addr(in)
		if err != nil || got != want {
			t.Errorf("%s: got %+v err=%v want %+v", in, got, err, want)
		}
	}
	for _, bad := range []string{"DB7.DBX28", "DB7.DBX28.9", "DB7.DBW0.1", "M0.0", "DB7", ""} {
		if _, err := parseS7Addr(bad); err == nil {
			t.Errorf("%q should be rejected", bad)
		}
	}
}

func db7Tags() []config.Tag {
	return []config.Tag{
		{Name: "Counter_Int", Type: "INT", S7Address: "DB7.DBW0"},
		{Name: "Ramp_Real", Type: "REAL", S7Address: "DB7.DBD2"},
		{Name: "Status_String", Type: "STRING[20]", S7Address: "DB7.DBB6"},
		{Name: "Tick_Bool", Type: "BOOL", S7Address: "DB7.DBX28.0"},
	}
}

func TestBuildPlanDB7OneSpan(t *testing.T) {
	p, err := buildPlan(db7Tags(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(p.spans))
	}
	sp := p.spans[0]
	if sp.db != 7 || sp.start != 0 || sp.length != 29 || len(sp.members) != 4 {
		t.Fatalf("span %+v", sp)
	}
	if sp.address() != "%DB7.DBB0[0..28]:BYTE" {
		t.Fatal(sp.address())
	}
}

func TestBuildPlanSplitsByDBGapAndSize(t *testing.T) {
	tags := []config.Tag{
		{Name: "a", Type: "INT", S7Address: "DB1.DBW0"},
		{Name: "b", Type: "INT", S7Address: "DB2.DBW0"},   // other DB
		{Name: "c", Type: "INT", S7Address: "DB1.DBW100"}, // gap > mergeGap
		{Name: "d", Type: "INT", S7Address: "DB1.DBW102"}, // joins c
	}
	p, err := buildPlan(tags, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.spans) != 3 {
		t.Fatalf("want 3 spans, got %+v", p.spans)
	}
	if p.spans[1].start != 100 || len(p.spans[1].members) != 2 || p.spans[1].length != 4 {
		t.Fatalf("span1 %+v", p.spans[1])
	}
	// size cap: chain of 4-byte tags every 4 bytes must split before maxSpan
	var many []config.Tag
	for i := 0; i < 400; i++ {
		many = append(many, config.Tag{Name: "t", Type: "DINT", S7Address: "DB1.DBD" + itoa(i*4)})
	}
	p, err = buildPlan(many, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, sp := range p.spans {
		if sp.length > defaultMaxSpan {
			t.Fatalf("span too long: %d", sp.length)
		}
	}
	if len(p.spans) < 2 {
		t.Fatal("expected split")
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	s := ""
	for ; i > 0; i /= 10 {
		s = string(rune('0'+i%10)) + s
	}
	return s
}

func TestBuildPlanRejectsBad(t *testing.T) {
	if _, err := buildPlan([]config.Tag{{Name: "x", Type: "INT", S7Address: "M0.0"}}, 0); err == nil {
		t.Fatal("bad address accepted")
	}
	if _, err := buildPlan([]config.Tag{{Name: "x", Type: "WSTRING", S7Address: "DB1.DBW0"}}, 0); err == nil {
		t.Fatal("bad type accepted")
	}
}

func TestDecodeDB7Image(t *testing.T) {
	buf := make([]byte, 29)
	buf[0], buf[1] = 0x00, 0x58                             // INT 88
	buf[2], buf[3], buf[4], buf[5] = 0x42, 0x30, 0x00, 0x00 // REAL 44.0
	buf[6], buf[7] = 20, 4                                  // STRING max 20, act 4
	copy(buf[8:], "IDLE")
	buf[28] = 0x01 // bit 0 set

	p, _ := buildPlan(db7Tags(), 0)
	got := map[string]any{}
	for _, m := range p.spans[0].members {
		v, err := decodeS7(m.typ, buf[m.rel:m.rel+m.size], m.bit)
		if err != nil {
			t.Fatal(m.tag.Name, err)
		}
		got[m.tag.Name] = v
	}
	if got["Counter_Int"] != int16(88) || got["Ramp_Real"] != float32(44.0) ||
		got["Status_String"] != "IDLE" || got["Tick_Bool"] != true {
		t.Fatalf("%#v", got)
	}
}

func TestDecodeTypes(t *testing.T) {
	cases := []struct {
		typ  string
		b    []byte
		bit  int
		want any
	}{
		{"INT", []byte{0xFF, 0xFE}, 0, int16(-2)},
		{"UINT", []byte{0xFF, 0xFE}, 0, uint16(0xFFFE)},
		{"DINT", []byte{0xFF, 0xFF, 0xFF, 0xFF}, 0, int32(-1)},
		{"DWORD", []byte{0, 0, 1, 0}, 0, uint32(256)},
		{"SINT", []byte{0x80}, 0, int8(-128)},
		{"BOOL", []byte{0x20}, 5, true},
		{"BOOL", []byte{0x20}, 4, false},
		{"STRING[4]", []byte{4, 9, 'a', 'b', 'c', 'd'}, 0, "abcd"}, // act_len clamped to max_len
	}
	for _, c := range cases {
		got, err := decodeS7(c.typ, c.b, c.bit)
		if err != nil || got != c.want {
			t.Errorf("%s %v: got %#v err=%v want %#v", c.typ, c.b, got, err, c.want)
		}
	}
	v, _ := decodeS7("LREAL", []byte{0x40, 0x09, 0x21, 0xFB, 0x54, 0x44, 0x2D, 0x18}, 0)
	if math.Abs(v.(float64)-math.Pi) > 1e-12 {
		t.Fatal(v)
	}
	if _, err := decodeS7("INT", []byte{1}, 0); err == nil {
		t.Fatal("short buffer accepted")
	}
}

func TestBuildPlanHonoursConfiguredMaxSpan(t *testing.T) {
	tags := []config.Tag{
		{Name: "a", Type: "DINT", S7Address: "DB1.DBD0"},
		{Name: "b", Type: "DINT", S7Address: "DB1.DBD8"}, // would merge by gap, but span 12 > 8
	}
	p, err := buildPlan(tags, 8)
	if err != nil || len(p.spans) != 2 {
		t.Fatalf("max_span=8 should split: %v %+v", err, p)
	}
	p, _ = buildPlan(tags, 0)
	if len(p.spans) != 1 {
		t.Fatalf("default should merge: %+v", p)
	}
}
