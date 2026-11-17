package driver

import (
	"encoding/binary"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"s7poc/internal/config"
)

// Merged read mode: instead of one plc4go read item per tag, tags that live close together in the
// same DB are fetched with ONE byte-range item and decoded locally (S7 is big-endian).

const (
	mergeGap = 16  // bytes: tags closer than this are read in one span (wasted bytes < extra item)
	maxSpan  = 900 // bytes per span: S7-1500 PDU is 960, minus S7 header/item overhead
)

var s7AddrRe = regexp.MustCompile(`(?i)^%?DB(\d+)\.DB([XBWD])(\d+)(?:\.(\d+))?$`)

type s7Loc struct{ DB, Offset, Bit int }

// parseS7Addr understands DB7.DBW0, DB7.DBX28.0, %DB10.DBD14:REAL. The area letter is only
// validated; the size comes from the configured tag type.
func parseS7Addr(addr string) (s7Loc, error) {
	a := strings.TrimSpace(addr)
	if i := strings.Index(a, ":"); i >= 0 {
		a = a[:i]
	}
	m := s7AddrRe.FindStringSubmatch(a)
	if m == nil {
		return s7Loc{}, fmt.Errorf("unsupported S7 address %q (expected DB<n>.DB<X|B|W|D><offset>)", addr)
	}
	db, _ := strconv.Atoi(m[1])
	off, _ := strconv.Atoi(m[3])
	bit := 0
	if strings.EqualFold(m[2], "X") {
		if m[4] == "" {
			return s7Loc{}, fmt.Errorf("bit address %q needs a bit number (.0-.7)", addr)
		}
		bit, _ = strconv.Atoi(m[4])
		if bit > 7 {
			return s7Loc{}, fmt.Errorf("bit number out of range in %q", addr)
		}
	} else if m[4] != "" {
		return s7Loc{}, fmt.Errorf("unexpected bit number in %q", addr)
	}
	return s7Loc{DB: db, Offset: off, Bit: bit}, nil
}

// s7TypeSize returns the number of bytes a value of the configured type occupies in a DB.
func s7TypeSize(typ string) (int, error) {
	t := strings.ToUpper(strings.TrimSpace(typ))
	if m := sizedType.FindStringSubmatch(t); m != nil {
		n, _ := strconv.Atoi(m[1])
		if n <= 0 || n > 254 {
			return 0, fmt.Errorf("STRING length out of range in %q", typ)
		}
		return n + 2, nil
	}
	switch t {
	case "BOOL", "BYTE", "SINT", "USINT":
		return 1, nil
	case "INT", "UINT", "WORD":
		return 2, nil
	case "DINT", "UDINT", "DWORD", "REAL", "TIME":
		return 4, nil
	case "LINT", "ULINT", "LWORD", "LREAL":
		return 8, nil
	}
	return 0, fmt.Errorf("type %q not supported in merged read mode", typ)
}

// decodeS7String decodes an S7 STRING buffer: [max_len, act_len, chars...].
func decodeS7String(b []byte) string {
	if len(b) < 2 {
		return ""
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

// decodeS7 decodes b (exactly the bytes of one value) into the same Go types convertTyped returns.
func decodeS7(typ string, b []byte, bit int) (any, error) {
	t := strings.ToUpper(strings.TrimSpace(typ))
	size, err := s7TypeSize(t)
	if err != nil {
		return nil, err
	}
	if len(b) < size {
		return nil, fmt.Errorf("need %d bytes for %s, have %d", size, t, len(b))
	}
	switch t {
	case "BOOL":
		return b[0]&(1<<uint(bit)) != 0, nil
	case "SINT":
		return int8(b[0]), nil
	case "USINT", "BYTE":
		return b[0], nil
	case "INT":
		return int16(binary.BigEndian.Uint16(b)), nil
	case "UINT", "WORD":
		return binary.BigEndian.Uint16(b), nil
	case "DINT", "TIME":
		return int32(binary.BigEndian.Uint32(b)), nil
	case "UDINT", "DWORD":
		return binary.BigEndian.Uint32(b), nil
	case "REAL":
		return math.Float32frombits(binary.BigEndian.Uint32(b)), nil
	case "LINT":
		return int64(binary.BigEndian.Uint64(b)), nil
	case "ULINT", "LWORD":
		return binary.BigEndian.Uint64(b), nil
	case "LREAL":
		return math.Float64frombits(binary.BigEndian.Uint64(b)), nil
	}
	return decodeS7String(b[:size]), nil // sized STRING (checked by s7TypeSize)
}

type s7Member struct {
	tag  config.Tag
	typ  string // upper-case configured type
	rel  int    // byte offset inside the span
	size int
	bit  int
}

type s7Span struct {
	name    string
	db      int
	start   int
	length  int
	members []s7Member
}

// address is the plc4go byte-array item for the whole span.
func (sp s7Span) address() string {
	return fmt.Sprintf("%%DB%d.DBB%d[0..%d]:BYTE", sp.db, sp.start, sp.length-1)
}

type s7Plan struct{ spans []s7Span }

// buildPlan groups the S7 tags into spans: same DB, gap <= mergeGap, span <= maxSpan.
func buildPlan(tags []config.Tag) (*s7Plan, error) {
	type item struct {
		tag  config.Tag
		typ  string
		loc  s7Loc
		size int
	}
	var items []item
	for _, t := range tags {
		if t.S7Address == "" {
			continue
		}
		loc, err := parseS7Addr(t.S7Address)
		if err != nil {
			return nil, fmt.Errorf("tag %s: %w", t.Name, err)
		}
		size, err := s7TypeSize(t.Type)
		if err != nil {
			return nil, fmt.Errorf("tag %s: %w", t.Name, err)
		}
		items = append(items, item{t, strings.ToUpper(strings.TrimSpace(t.Type)), loc, size})
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].loc.DB != items[j].loc.DB {
			return items[i].loc.DB < items[j].loc.DB
		}
		return items[i].loc.Offset < items[j].loc.Offset
	})
	p := &s7Plan{}
	ci := -1
	for _, it := range items {
		end := it.loc.Offset + it.size
		if ci >= 0 {
			cur := &p.spans[ci]
			curEnd := cur.start + cur.length
			newEnd := curEnd
			if end > newEnd {
				newEnd = end
			}
			if cur.db == it.loc.DB && it.loc.Offset <= curEnd+mergeGap && newEnd-cur.start <= maxSpan {
				cur.length = newEnd - cur.start
				cur.members = append(cur.members, s7Member{it.tag, it.typ, it.loc.Offset - cur.start, it.size, it.loc.Bit})
				continue
			}
		}
		p.spans = append(p.spans, s7Span{db: it.loc.DB, start: it.loc.Offset, length: it.size})
		ci = len(p.spans) - 1
		p.spans[ci].members = append(p.spans[ci].members, s7Member{it.tag, it.typ, 0, it.size, it.loc.Bit})
	}
	for i := range p.spans {
		p.spans[i].name = fmt.Sprintf("span%d", i)
	}
	return p, nil
}
