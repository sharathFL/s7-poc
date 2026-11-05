package driver

import "testing"

func TestToPlc4xAddress(t *testing.T) {
	cases := [][3]string{
		{"DB10.DBD0", "REAL", "%DB10.DBD0:REAL"},
		{"DB10.DBX12.0", "bool", "%DB10.DBX12.0:BOOL"},
		{"%DB10.DBD14", "DINT", "%DB10.DBD14:DINT"},
		{"DB10.DBW2:INT", "INT", "%DB10.DBW2:INT"},
	}
	for _, c := range cases {
		if got := ToPlc4xAddress(c[0], c[1]); got != c[2] {
			t.Errorf("%s: got %s want %s", c[0], got, c[2])
		}
	}
}

func TestSimulatedDeterministic(t *testing.T) {
	if SimulatedValue("Temperature", "REAL", 100) != SimulatedValue("Temperature", "REAL", 100) {
		t.Fatal("not deterministic")
	}
}
