package routes

import (
	"agent/netfilter"
	"context"
)

// Route tests never inspect the real host firewall.
func init() {
	forwardFilterInspect = func(context.Context, netfilter.Target) netfilter.Result {
		return netfilter.Result{Protocol: 1, State: netfilter.StateOK, Policy: "accept"}
	}
}
