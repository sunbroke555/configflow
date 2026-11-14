package agents

// Extension points for the packages that build on the registry. Python
// imported these modules lazily from manager.py and routes/agents.py; here the
// owning package registers its part from its own wire function. Every hook
// has the default described on its field: either the exact Python behaviour
// for the case the later package is not involved in, or a fail-closed error.

import (
	"context"
	"errors"
	"sync"

	"configflow/internal/jsonx"
)

// PublishOptions are the keyword arguments of publish_deployment.
type PublishOptions struct {
	Activate       bool
	ConfigRevision any // int or nil
	BaseURL        string
	ResourceConfig *jsonx.Object
	SourceSnapshot *jsonx.Object
}

// InstallationError is gateway_installation.InstallationError (#30).
type InstallationError struct {
	Message string
	Status  int
}

func (e *InstallationError) Error() string { return e.Message }

// Hooks are the parts of other packages the registry calls.
type Hooks struct {
	// AddMetrics is metrics_history.add_metrics (#21), called for every
	// heartbeat that carries system_metrics; an error is logged as a warning.
	// Default: nothing is kept.
	AddMetrics func(m *Manager, agentID string, metrics any) error
	// DeleteMetricsHistory is metrics_history.delete_agent_history (#21).
	DeleteMetricsHistory func(m *Manager, agentID string)
	// DeleteTrafficHistory is traffic_history.delete (#21); an error is
	// logged as a warning.
	DeleteTrafficHistory func(m *Manager, agentID string) error

	// PublicationComplete and PublicationCheckpoint are
	// oxidns_publication_stats.complete / checkpoint (#21). Default: nothing.
	PublicationComplete   func(ctx context.Context, m *Manager, agentID, deploymentID string, status any) error
	PublicationCheckpoint func(ctx context.Context, m *Manager, agentID, deploymentID string, configSHA256 any) error

	// AddressFollow and AddressConfirmed are address_follow.follow and
	// address_follow.confirmed (#28), used by the heartbeat route when the
	// Agent reports its host. Default: nothing followed, no confirmed_host.
	AddressFollow    func(ctx context.Context, m *Manager, agent *jsonx.Object, host string) error
	AddressConfirmed func(agent *jsonx.Object, host string) string

	// ReceiptMatches is oxidns_validation_receipt.receipt_matches (#28),
	// checked before an OxiDNS candidate is activated. Default: false (fail
	// closed: activation is refused and the task fails as unchecked).
	ReceiptMatches func(agent, state *jsonx.Object) bool

	// HAServiceOperation, HADeployment and HAActivate are
	// oxidns_ha_publish.service_operation / deployment / activate (#29).
	// Defaults: the Python code path for Agents without an active pair
	// publication (conflict checks included, without the pair lock file);
	// a deployment that belongs to a pair publication fails with
	// ErrNotPorted.
	HAServiceOperation func(ctx context.Context, m *Manager, agentID string, operation func() (*jsonx.Object, error), uninstall bool) (*jsonx.Object, error)
	HADeployment       func(ctx context.Context, m *Manager, agentID, deploymentID string) (*jsonx.Object, error)
	HAActivate         func(ctx context.Context, m *Manager, agentID, deploymentID string) (*jsonx.Object, error)

	// PublishDeployment is publish_deployment (oxidns_ha_publish.publish
	// around _publish_deployment, #24/#26/#29). bundle is the deployment
	// bundle of #24. Default: ErrNotPorted.
	PublishDeployment func(ctx context.Context, m *Manager, agentID string, bundle any, options PublishOptions) (*jsonx.Object, error)

	// InstallationProfile and InstallationRegister are
	// gateway_installation.registration_profile / register (#30), used by
	// the registration route for gateway installs. Return *InstallationError
	// for the user-facing refusals. Default: an InstallationError (501).
	InstallationProfile  func(ctx context.Context, data *jsonx.Object, installationID, installationToken any, fallbackHost string) (any, error)
	InstallationRegister func(ctx context.Context, m *Manager, installationID, installationToken any, data *jsonx.Object, existingToken any) (*jsonx.Object, error)
}

// ErrNotPorted is returned where the registry would call a later package
// whose hook is not registered yet.
var ErrNotPorted = errors.New("agents: the package providing this operation is not ported yet")

var (
	hooksMu sync.RWMutex
	hooks   Hooks
)

// RegisterHooks installs the non-nil fields.
func RegisterHooks(h Hooks) {
	hooksMu.Lock()
	defer hooksMu.Unlock()
	if h.AddMetrics != nil {
		hooks.AddMetrics = h.AddMetrics
	}
	if h.DeleteMetricsHistory != nil {
		hooks.DeleteMetricsHistory = h.DeleteMetricsHistory
	}
	if h.DeleteTrafficHistory != nil {
		hooks.DeleteTrafficHistory = h.DeleteTrafficHistory
	}
	if h.PublicationComplete != nil {
		hooks.PublicationComplete = h.PublicationComplete
	}
	if h.PublicationCheckpoint != nil {
		hooks.PublicationCheckpoint = h.PublicationCheckpoint
	}
	if h.AddressFollow != nil {
		hooks.AddressFollow = h.AddressFollow
	}
	if h.AddressConfirmed != nil {
		hooks.AddressConfirmed = h.AddressConfirmed
	}
	if h.ReceiptMatches != nil {
		hooks.ReceiptMatches = h.ReceiptMatches
	}
	if h.HAServiceOperation != nil {
		hooks.HAServiceOperation = h.HAServiceOperation
	}
	if h.HADeployment != nil {
		hooks.HADeployment = h.HADeployment
	}
	if h.HAActivate != nil {
		hooks.HAActivate = h.HAActivate
	}
	if h.PublishDeployment != nil {
		hooks.PublishDeployment = h.PublishDeployment
	}
	if h.InstallationProfile != nil {
		hooks.InstallationProfile = h.InstallationProfile
	}
	if h.InstallationRegister != nil {
		hooks.InstallationRegister = h.InstallationRegister
	}
}

// ResetHooks clears every hook (tests).
func ResetHooks() {
	hooksMu.Lock()
	hooks = Hooks{}
	hooksMu.Unlock()
}

func currentHooks() Hooks {
	hooksMu.RLock()
	defer hooksMu.RUnlock()
	return hooks
}

// CurrentHooks returns the installed hooks (for routes in other packages).
func CurrentHooks() Hooks { return currentHooks() }

var (
	listMu      sync.Mutex
	deleteHooks []namedDelete
	initHooks   []func(ctx context.Context, m *Manager)
)

type namedDelete struct {
	name string
	fn   func(ctx context.Context, agentID string) error
}

// OnAgentDeleted adds a store the DELETE /api/agents/<id> route clears after
// the registry deleted the Agent. Python's order is traffic_store (#21),
// domain_probe (#14), region_check (#14); an error answers 500 with its text.
// Registering the same name again replaces the function in place.
func OnAgentDeleted(name string, fn func(ctx context.Context, agentID string) error) {
	listMu.Lock()
	defer listMu.Unlock()
	for i, item := range deleteHooks {
		if item.name == name {
			deleteHooks[i].fn = fn
			return
		}
	}
	deleteHooks = append(deleteHooks, namedDelete{name, fn})
}

// AgentDeletedHooks returns the registered delete functions in order.
func AgentDeletedHooks() []func(ctx context.Context, agentID string) error {
	listMu.Lock()
	defer listMu.Unlock()
	out := make([]func(ctx context.Context, agentID string) error, len(deleteHooks))
	for i, item := range deleteHooks {
		out[i] = item.fn
	}
	return out
}

// OnManagerInit adds work init_agent_manager does after creating the
// manager: dns_follow.start_loop (#28), oxidns_ha_publish.start_loop (#29),
// ipv6_verification.start_loop (#31), in that order of registration.
func OnManagerInit(fn func(ctx context.Context, m *Manager)) {
	listMu.Lock()
	initHooks = append(initHooks, fn)
	listMu.Unlock()
}

func managerInitHooks() []func(ctx context.Context, m *Manager) {
	listMu.Lock()
	defer listMu.Unlock()
	return append([]func(ctx context.Context, m *Manager){}, initHooks...)
}
