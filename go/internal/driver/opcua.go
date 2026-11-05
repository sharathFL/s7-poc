package driver

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/gopcua/opcua"
	"github.com/gopcua/opcua/id"
	"github.com/gopcua/opcua/ua"

	"s7poc/internal/config"
	"s7poc/internal/model"
)

// OpcUa is READ-ONLY: only Read and Browse services are used. Subscriptions are Python-only.
type OpcUa struct {
	dev  config.Device
	cfg  config.OpcUa
	mu   sync.Mutex
	c    *opcua.Client
	ids  []*ua.NodeID
	tags []config.Tag
}

func NewOpcUa(d config.Device) *OpcUa { return &OpcUa{dev: d, cfg: d.OpcUa} }

func (o *OpcUa) Protocol() string { return "opcua" }
func (o *OpcUa) Kind() string     { return "opcua" }
func (o *OpcUa) PollingMS() int   { return o.cfg.PollingMS }
func (o *OpcUa) Connected() bool  { o.mu.Lock(); defer o.mu.Unlock(); return o.c != nil }

func (o *OpcUa) secure() bool {
	return !strings.EqualFold(o.cfg.SecurityMode, "none") && !strings.EqualFold(o.cfg.SecurityPolicy, "none")
}

func (o *OpcUa) Connect(ctx context.Context) error {
	o.Close()
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.cfg.TimeoutMS*3)*time.Millisecond)
	defer cancel()
	opts := []opcua.Option{opcua.RequestTimeout(time.Duration(o.cfg.TimeoutMS) * time.Millisecond)}
	tokType := ua.UserTokenTypeAnonymous
	if o.cfg.Username != "" {
		tokType = ua.UserTokenTypeUserName
		opts = append(opts, opcua.AuthUsername(o.cfg.Username, o.cfg.Password))
	} else {
		opts = append(opts, opcua.AuthAnonymous())
	}
	if o.secure() {
		if o.cfg.ClientCert == "" || o.cfg.ClientKey == "" {
			return fmt.Errorf("secure OPC UA needs client_cert and client_key (mounted under /secrets)")
		}
		eps, err := opcua.GetEndpoints(ctx, o.cfg.Endpoint)
		if err != nil {
			return fmt.Errorf("%w: OPC UA GetEndpoints %s: %v", ErrComm, o.cfg.Endpoint, err)
		}
		ep, err := opcua.SelectEndpoint(eps, o.cfg.SecurityPolicy, ua.MessageSecurityModeFromString(o.cfg.SecurityMode))
		if err != nil {
			return fmt.Errorf("no matching endpoint for %s/%s: %v", o.cfg.SecurityPolicy, o.cfg.SecurityMode, err)
		}
		opts = append(opts, opcua.SecurityFromEndpoint(ep, tokType),
			opcua.CertificateFile(o.cfg.ClientCert), opcua.PrivateKeyFile(o.cfg.ClientKey))
	} else {
		opts = append(opts, opcua.SecurityMode(ua.MessageSecurityModeNone))
	}
	c, err := opcua.NewClient(o.cfg.Endpoint, opts...)
	if err != nil {
		return fmt.Errorf("OPC UA client: %w", err)
	}
	if err := c.Connect(ctx); err != nil {
		// never include credentials in the message
		return fmt.Errorf("%w: OPC UA connect to %s: %v", ErrComm, o.cfg.Endpoint, err)
	}
	o.mu.Lock()
	o.c, o.ids, o.tags = c, nil, nil
	for _, t := range o.dev.Tags {
		if t.OpcuaNode == "" {
			continue
		}
		nid, err := ua.ParseNodeID(t.OpcuaNode)
		if err != nil {
			o.mu.Unlock()
			_ = c.Close(ctx)
			return fmt.Errorf("tag %s: bad node id %q: %v", t.Name, t.OpcuaNode, err)
		}
		o.ids, o.tags = append(o.ids, nid), append(o.tags, t)
	}
	o.mu.Unlock()
	return nil
}

func (o *OpcUa) Close() {
	o.mu.Lock()
	c := o.c
	o.c = nil
	o.mu.Unlock()
	if c != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = c.Close(ctx)
	}
}

func typeName(v any) string {
	switch v.(type) {
	case float32:
		return "Float"
	case float64:
		return "Double"
	case bool:
		return "Boolean"
	case int8:
		return "SByte"
	case uint8:
		return "Byte"
	case int16:
		return "Int16"
	case uint16:
		return "UInt16"
	case int32:
		return "Int32"
	case uint32:
		return "UInt32"
	case int64:
		return "Int64"
	case uint64:
		return "UInt64"
	case string:
		return "String"
	}
	return fmt.Sprintf("%T", v)
}

func (o *OpcUa) Read(ctx context.Context) ([]model.Reading, error) {
	o.mu.Lock()
	c, ids, tags := o.c, o.ids, o.tags
	o.mu.Unlock()
	if c == nil {
		return nil, fmt.Errorf("%w: OPC UA not connected", ErrComm)
	}
	req := &ua.ReadRequest{TimestampsToReturn: ua.TimestampsToReturnBoth}
	for _, n := range ids {
		req.NodesToRead = append(req.NodesToRead, &ua.ReadValueID{NodeID: n, AttributeID: ua.AttributeIDValue})
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.cfg.TimeoutMS)*time.Millisecond)
	defer cancel()
	t0 := time.Now()
	resp, err := c.Read(ctx, req)
	lat := float64(time.Since(t0).Microseconds()) / 1000
	if err != nil {
		return nil, fmt.Errorf("%w: OPC UA read failed: %v", ErrComm, err)
	}
	now := time.Now()
	var out []model.Reading
	for i, dv := range resp.Results {
		if i >= len(tags) {
			break
		}
		t := tags[i]
		status := "Good"
		if dv.Status != ua.StatusOK {
			status = dv.Status.Error()
		}
		md := map[string]any{"node_id": t.OpcuaNode, "status": status}
		if !dv.SourceTimestamp.IsZero() {
			md["source_timestamp"] = model.FormatTime(dv.SourceTimestamp)
		}
		if !dv.ServerTimestamp.IsZero() {
			md["server_timestamp"] = model.FormatTime(dv.ServerTimestamp)
		}
		r := model.Reading{DeviceID: o.dev.ID, Protocol: "opcua", Tag: t.Name, Timestamp: now, LatencyMS: &lat, Metadata: md}
		if dv.Status != ua.StatusOK || dv.Value == nil {
			r.Quality, r.Datatype, r.Error = model.Invalid, "?", "status "+status
		} else {
			v := dv.Value.Value()
			r.Quality, r.Datatype, r.Value = model.Good, typeName(v), v
			switch v.(type) {
			case bool, string, int8, uint8, int16, uint16, int32, uint32, int64, uint64, float32, float64:
			default:
				r.Value = fmt.Sprint(v)
			}
		}
		out = append(out, r)
	}
	return out, nil
}

func (o *OpcUa) Browse(ctx context.Context, start string, depth int) ([]map[string]any, error) {
	o.mu.Lock()
	c := o.c
	o.mu.Unlock()
	if c == nil {
		return nil, fmt.Errorf("%w: OPC UA not connected", ErrComm)
	}
	if depth < 1 {
		depth = 1
	}
	if depth > 6 {
		depth = 6
	}
	root := ua.NewNumericNodeID(0, id.ObjectsFolder)
	if start != "" {
		n, err := ua.ParseNodeID(start)
		if err != nil {
			return nil, fmt.Errorf("bad start node: %w", err)
		}
		root = n
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var out []map[string]any
	var walk func(n *opcua.Node, d int, path string) error
	walk = func(n *opcua.Node, d int, path string) error {
		kids, err := n.ReferencedNodes(ctx, id.HierarchicalReferences, ua.BrowseDirectionForward, ua.NodeClassAll, true)
		if err != nil {
			return err
		}
		for _, k := range kids {
			bn, err := k.BrowseName(ctx)
			if err != nil {
				continue
			}
			nc, _ := k.NodeClass(ctx)
			p := path + "/" + bn.Name
			out = append(out, map[string]any{"node_id": k.ID.String(), "browse_name": bn.Name,
				"node_class": strings.TrimPrefix(nc.String(), "NodeClass"), "path": p})
			if d > 1 && len(out) < 2000 && (nc == ua.NodeClassObject || nc == ua.NodeClassVariable) {
				_ = walk(k, d-1, p)
			}
		}
		return nil
	}
	if err := walk(c.Node(root), depth, ""); err != nil {
		return nil, err
	}
	return out, nil
}
