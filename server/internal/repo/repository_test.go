package repo

// Port of backend/test_config_repository.py: persistence boundaries,
// independent writers and recoverable imports.

import (
	"context"
	"database/sql"
	"strings"
	"sync"
	"testing"

	"configflow/internal/jsonx"
)

// rcDefaultRepo is ProfileRepository(root, get_default_config).
func rcDefaultRepo(t *testing.T, dataDir string) *Repository {
	t.Helper()
	return must(Open(dataDir, Options{DefaultConfig: GetDefaultConfig}))(t)
}

// rcUpd turns an in-place edit into an Updater.
func rcUpd(edit func(current *jsonx.Object)) Updater {
	return func(_ context.Context, current *jsonx.Object) (*jsonx.Object, error) {
		edit(current)
		return nil, nil
	}
}

// rcAppend is obj[key].append(items...).
func rcAppend(obj *jsonx.Object, key string, items ...any) {
	obj.Set(key, append(append([]any{}, obj.GetList(key)...), items...))
}

// rcAt is list[index] as an object.
func rcAt(t testing.TB, list any, index int) *jsonx.Object {
	t.Helper()
	items, ok := jsonx.AsList(list)
	if !ok || index >= len(items) {
		t.Fatalf("no item %d in %s", index, jsonx.Repr(list))
	}
	object, ok := jsonx.AsObject(items[index])
	if !ok {
		t.Fatalf("item %d is not an object: %s", index, jsonx.Repr(items[index]))
	}
	return object
}

// rcList parses a JSON array literal.
func rcList(t testing.TB, text string) []any {
	t.Helper()
	return parse(t, `{"v":`+text+`}`).GetList("v")
}

// rcSQL runs statements on the database behind the repository's back.
func rcSQL(t testing.TB, path string, statements ...string) {
	t.Helper()
	db := must(sql.Open("sqlite", "file:"+path))(t)
	defer db.Close()
	for _, statement := range statements {
		if _, err := db.Exec(statement); err != nil {
			t.Fatalf("%s: %v", statement, err)
		}
	}
}

// rcPragma reads one pragma through a plain connection.
func rcPragma(t testing.TB, path, statement string) string {
	t.Helper()
	db := must(sql.Open("sqlite", "file:"+path))(t)
	defer db.Close()
	var value any
	noError(t, db.QueryRow(statement).Scan(&value))
	switch typed := value.(type) {
	case []byte:
		return string(typed)
	case string:
		return typed
	case int64:
		return jsonx.Int(typed).String()
	}
	t.Fatalf("unexpected pragma value %T", value)
	return ""
}

func rcLegacy(t testing.TB, hostname string) *jsonx.Object {
	t.Helper()
	data := GetDefaultConfig()
	extra := parse(t, strings.ReplaceAll(`{
		"subscriptions": [{"id": "sub", "name": "Source", "url": "https://HOST/subscription"}],
		"nodes": [{"id": "node", "name": "Node", "server": "HOST", "type": "socks5", "port": 1080}],
		"subscription_aggregations": [{"id": "agg", "name": "Aggregate", "subscriptions": ["sub"], "nodes": ["node"]}],
		"rule_library": [{"id": "lib", "name": "Domains", "source_type": "content", "content": "HOST", "behavior": "domain"}],
		"proxy_groups": [{"id": "group", "name": "Proxy", "type": "select", "subscriptions": ["sub"], "manual_nodes": ["node", "DIRECT"], "aggregations": ["agg"]}],
		"rule_configs": [
			{"id": "r1", "itemType": "ruleset", "library_rule_id": "lib", "name": "Domains", "behavior": "domain", "policy": "DIRECT"},
			{"id": "r2", "itemType": "rule", "rule_type": "MATCH", "policy": "Proxy"}
		]}`, "HOST", hostname))
	for key, value := range extra.All() {
		data.Set(key, value)
	}
	data.GetObject("mosdns").Set("direct_rulesets", []any{"r1"})
	return data
}

func TestOriginalInlineRulesBecomeSharedSourcesWithoutChangingPolicies(t *testing.T) {
	legacy := rcLegacy(t, "one.example")
	rcAppend(legacy, "rule_configs", parse(t, `{"id": "inline", "itemType": "ruleset", "name": "Domains",
		"content": "private.example", "behavior": "domain", "policy": "Proxy", "enabled": false}`))
	r := rcDefaultRepo(t, t.TempDir())
	must(r.ImportAll(bg, legacy))(t)
	resolved := must(r.GetCompatConfig(bg, "default", nil))(t)
	rules := map[string]*jsonx.Object{}
	for _, item := range resolved.GetList("rule_configs") {
		rule := item.(*jsonx.Object)
		rules[rule.GetString("id")] = rule
	}
	wantEqual(t, rules["r1"].Value("content"), "one.example")
	wantEqual(t, rules["inline"].Value("content"), "private.example")
	wantEqual(t, rules["inline"].Value("policy"), "Proxy")
	wantEqual(t, rules["inline"].Value("enabled"), false)
	if jsonx.Equal(rules["inline"].Value("library_rule_id"), rules["r1"].Value("library_rule_id")) {
		t.Fatal("inline rule shares the library entry of r1")
	}
	stored := must(r.GetProfile(bg, "default"))(t).GetList("rule_configs")
	if rcAt(t, stored, len(stored)-1).Has("content") {
		t.Fatal("content persisted in the profile")
	}
}

func TestUnsupportedImportPreservesExistingDataAndOriginalFile(t *testing.T) {
	r := rcDefaultRepo(t, t.TempDir())
	must(r.ImportAll(bg, rcLegacy(t, "one.example")))(t)
	before := must(r.Store().Fingerprint())(t)
	invalid := must(r.ExportAll(bg, false))(t)
	invalid.Set("schema_version", 2)
	_, err := r.ImportAll(bg, invalid)
	wantError(t, err, ErrValidation, "")
	if must(r.Store().Fingerprint())(t) != before {
		t.Fatal("database changed")
	}
}

func TestIndependentConcurrentProfileWritesAndSharedUpdatesSurvive(t *testing.T) {
	root := t.TempDir()
	r := rcDefaultRepo(t, root)
	must(r.CreateProfile(bg, jsonx.Obj("name", "Second", "id", "second"), ""))(t)
	otherWriter := rcDefaultRepo(t, root)
	one := must(r.GetProfile(bg, "default"))(t)
	two := must(otherWriter.GetProfile(bg, "second"))(t)
	one.GetObject("mihomo").Set("custom_config", "mixed-port: 7901")
	two.GetObject("mihomo").Set("custom_config", "mixed-port: 7902")
	shared := must(r.GetShared(bg))(t)
	shared.Set("nodes", rcList(t, `[{"id": "node", "name": "Node", "server": "new.example", "port": 1080, "type": "socks5"}]`))
	var group sync.WaitGroup
	errs := make([]error, 3)
	jobs := []func() error{
		func() error { _, _, err := r.SaveProfile(bg, "default", one, nil); return err },
		func() error { _, _, err := otherWriter.SaveProfile(bg, "second", two, nil); return err },
		func() error { _, err := r.SaveShared(bg, shared, nil); return err },
	}
	for index, job := range jobs {
		group.Add(1)
		go func() { defer group.Done(); errs[index] = job() }()
	}
	group.Wait()
	for _, err := range errs {
		noError(t, err)
	}
	wantEqual(t, jsonx.Path(must(r.GetCompatConfig(bg, "default", nil))(t), "mihomo", "custom_config"), "mixed-port: 7901")
	wantEqual(t, jsonx.Path(must(r.GetCompatConfig(bg, "second", nil))(t), "mihomo", "custom_config"), "mixed-port: 7902")
	wantEqual(t, rcAt(t, must(r.GetShared(bg))(t).Value("nodes"), 0).Value("server"), "new.example")
	_, _, err := r.SaveProfile(bg, "default", one, nil)
	wantError(t, err, ErrInUse, "")
}

func TestImportIsWholeSystemAtomicAndInvalidatesOldSnapshots(t *testing.T) {
	r := rcDefaultRepo(t, t.TempDir())
	must(r.ImportAll(bg, rcLegacy(t, "one.example")))(t)
	must(r.CreateProfile(bg, jsonx.Obj("name", "Second", "id", "second"), "default"))(t)
	system := must(r.GetSystem(bg))(t)
	system.Set("agents", rcList(t, `[{"id": "agent", "profile_id": "second", "token": "secret"}]`))
	system.Set("backup", jsonx.Obj("webdav_password", "restore-me"))
	must(r.SaveSystem(bg, system, nil))(t)
	backup := must(r.ExportAll(bg, false))(t)
	stale := must(r.GetProfile(bg, "default"))(t)
	changed := must(r.GetProfile(bg, "second"))(t)
	changed.GetObject("mihomo").Set("custom_config", "mixed-port: 9900")
	_, _, err := r.SaveProfile(bg, "second", changed, nil)
	noError(t, err)
	invalid := backup.DeepCopy()
	rcAt(t, jsonx.Path(invalid, "profiles", "second", "proxy_groups"), 0).Set("manual_nodes", []any{"missing"})
	before := must(r.ExportAll(bg, false))(t)
	_, err = r.ImportAll(bg, invalid)
	wantError(t, err, ErrInUse, "")
	wantEqual(t, must(r.ExportAll(bg, false))(t), before)
	must(r.ImportAll(bg, backup))(t)
	wantEqual(t, must(r.GetProfile(bg, "second"))(t).Value("mihomo"), jsonx.Path(backup, "profiles", "second", "mihomo"))
	wantEqual(t, must(r.GetSystem(bg))(t).Value("agents"), jsonx.Path(backup, "system", "agents"))
	wantEqual(t, jsonx.Path(must(r.GetSystem(bg))(t), "backup", "webdav_password"), "restore-me")
	_, _, err = r.SaveProfile(bg, "default", stale, nil)
	wantError(t, err, ErrInUse, "")
}

func TestProfileOnlyAcceptsReferenceIdsAndNeverPersistsSourceCopies(t *testing.T) {
	r := rcDefaultRepo(t, t.TempDir())
	must(r.ImportAll(bg, rcLegacy(t, "one.example")))(t)
	snapshot := must(r.GetCompatConfig(bg, "default", nil))(t)
	rcAt(t, snapshot.Value("nodes"), 0).Set("server", "override.example")
	rcAt(t, snapshot.Value("rule_configs"), 0).Set("content", "override.example")
	_, _, err := r.SaveProfile(bg, "default", snapshot, nil)
	noError(t, err)
	resolved := must(r.GetCompatConfig(bg, "default", nil))(t)
	wantEqual(t, rcAt(t, resolved.Value("nodes"), 0).Value("server"), "one.example")
	wantEqual(t, rcAt(t, resolved.Value("rule_configs"), 0).Value("content"), "one.example")
	raw := must(r.GetProfile(bg, "default"))(t)
	if raw.Has("nodes") || rcAt(t, raw.Value("rule_configs"), 0).Has("content") {
		t.Fatal("source copies persisted")
	}
	rcAt(t, raw.Value("proxy_groups"), 0).Set("manual_nodes", rcList(t, `[{"id": "node", "server": "override.example"}]`))
	_, _, err = r.SaveProfile(bg, "default", raw, nil)
	wantError(t, err, ErrValidation, "")
}

func TestSharedDeletionAndDisableReportsAllProfileUsage(t *testing.T) {
	r := newRepo(t)
	must(r.UpdateSharedTransaction(bg, rcUpd(func(shared *jsonx.Object) {
		rcAppend(shared, "nodes", jsonx.Obj("id", "n", "name", "Node"))
	})))(t)
	_, _, err := r.UpdateProfileFields(bg, "default", parse(t, `{"proxy_groups": [
		{"id": "choice", "name": "Choice", "type": "select", "manual_nodes": ["n"]}]}`), nil)
	noError(t, err)
	must(r.CloneProfile(bg, "default", jsonx.Obj("id", "other", "name", "Other")))(t)
	updaters := []Updater{
		rcUpd(func(shared *jsonx.Object) { shared.Set("nodes", []any{}) }),
		rcUpd(func(shared *jsonx.Object) { rcAt(t, shared.Value("nodes"), 0).Set("enabled", false) }),
	}
	for _, updater := range updaters {
		_, err := r.UpdateSharedTransaction(bg, updater)
		wantError(t, err, ErrInUse, "")
		ids := map[string]bool{}
		for _, usage := range UsagesOf(err) {
			ids[usage.(*jsonx.Object).GetString("id")] = true
		}
		if len(ids) != 2 || !ids["default"] || !ids["other"] {
			t.Fatalf("usages %s", jsonx.Repr(UsagesOf(err)))
		}
	}
}

func TestSystemTransactionsValidateNestedProfileReadsAndBindings(t *testing.T) {
	r := newRepo(t)
	must(r.CreateProfile(bg, jsonx.Obj("id", "bound", "name", "Bound"), ""))(t)
	must(r.UpdateSystemTransaction(bg, func(ctx context.Context, system *jsonx.Object) (*jsonx.Object, error) {
		metadata, err := r.ProfileMetadata(ctx, "bound")
		if err != nil {
			return nil, err
		}
		wantEqual(t, metadata.Value("name"), "Bound")
		rcAppend(system, "agents", jsonx.Obj("id", "a", "name", "Agent", "profile_id", "bound"))
		return nil, nil
	}))(t)
	wantError(t, r.DeleteProfile(bg, "bound", ""), ErrInUse, "")
	before := must(r.ExportAll(bg, false))(t)
	_, err := r.UpdateSystemTransaction(bg, rcUpd(func(system *jsonx.Object) {
		rcAt(t, system.Value("agents"), 0).Set("profile_id", "missing")
	}))
	wantError(t, err, ErrValidation, "")
	wantEqual(t, must(r.ExportAll(bg, false))(t), before)
	keys := must(r.GetSystem(bg))(t).Keys()
	wantEqual(t, jsonx.ListOf(keys), []any{"system_config", "backup", "agents", "_revision"})
}

func TestSharedCachePathsAreAtomicAndRejectEscape(t *testing.T) {
	r := newRepo(t)
	must(r.WriteSharedJSON(bg, "subscribes/sub.json", jsonx.Obj("nodes", []any{})))(t)
	wantEqual(t, must(r.ReadSharedJSON(bg, "subscribes/sub.json"))(t), jsonx.Obj("nodes", []any{}))
	must(r.WriteSharedText(bg, "rules/domains.txt", []byte("example.com")))(t)
	text := must(readFileString(must(r.SharedRulesDir())(t) + "/domains.txt"))(t)
	if text != "example.com" {
		t.Fatalf("content %q", text)
	}
	for _, path := range []string{"../escape.json", "/tmp/escape.json", ""} {
		_, err := r.WriteSharedJSON(bg, path, jsonx.NewObject())
		wantError(t, err, ErrValidation, "")
	}
}

func TestGlobalSettingsThreeWayMergePreservesConcurrentAgentUpdates(t *testing.T) {
	r := newRepo(t)
	ctx := WithScope(WithRepository(bg, r))
	snapshot := must(GetSystemConfig(ctx))(t)
	snapshot.GetObject("system_config").Set("server_domain", "https://configured.example")
	must(r.UpdateSystemTransaction(bg, rcUpd(func(system *jsonx.Object) {
		rcAppend(system, "agents", jsonx.Obj("id", "a", "profile_id", "default"))
	})))(t)
	noError(t, SaveSystemConfig(ctx, snapshot, nil))
	system := must(r.GetSystem(bg))(t)
	wantEqual(t, system.Value("agents"), rcList(t, `[{"id": "a", "profile_id": "default"}]`))
	wantEqual(t, jsonx.Path(system, "system_config", "server_domain"), "https://configured.example")
}

func rcRuleDNSRepository(t *testing.T) *Repository {
	t.Helper()
	r := newRepo(t)
	must(r.SaveShared(bg, parse(t, `{"rule_library": [
		{"id": "lib", "name": "Ads", "source_type": "content", "content": "ads.test", "behavior": "domain"}]}`), nil))(t)
	_, _, err := r.SaveProfile(bg, "default", parse(t, `{"rule_configs": [
		{"id": "rule", "itemType": "rule", "rule_type": "DOMAIN", "value": "a.test", "policy": "DIRECT"},
		{"id": "set", "itemType": "ruleset", "library_rule_id": "lib", "policy": "REJECT"}]}`), nil)
	noError(t, err)
	must(r.CloneProfile(bg, "default", jsonx.Obj("id", "other", "name", "Other")))(t)
	return r
}

func rcIDs(list any) []any {
	items, _ := jsonx.AsList(list)
	ids := []any{}
	for _, item := range items {
		ids = append(ids, item.(*jsonx.Object).Value("id"))
	}
	return ids
}

func rcOxidnsUpdate(fields *jsonx.Object) Updater {
	return rcUpd(func(profile *jsonx.Object) { profile.GetObject("oxidns").Update(fields.DeepCopy()) })
}

func TestSharedAndProfilesTransactionCommitsOnce(t *testing.T) {
	r := rcRuleDNSRepository(t)
	sharedRevision := must(r.GetShared(bg))(t).GetInt("_revision")
	revisions := map[string]int64{}
	for _, profileID := range []string{"default", "other"} {
		revisions[profileID] = must(r.GetProfile(bg, profileID))(t).GetInt("_revision")
	}
	seen := map[string][2][]any{}
	add := rcUpd(func(shared *jsonx.Object) {
		rcAppend(shared, "rule_library", parse(t, `{"id": "new", "name": "New", "source_type": "content",
			"content": "new.test", "behavior": "classical"}`))
	})
	compose := func(_ context.Context, profileID string, before, after *jsonx.Object) (*jsonx.Object, error) {
		// before is resolved against the shared data as it was, after against the new one.
		seen[profileID] = [2][]any{rcIDs(before.Value("rule_library")), rcIDs(after.Value("rule_library"))}
		rule := parse(t, `{"id": "ref", "itemType": "ruleset", "library_rule_id": "new", "policy": "DIRECT"}`)
		rule.Set("behavior", "classical") // Hydrated fields never persist.
		after.Set("rule_configs", append([]any{rule}, after.GetList("rule_configs")...))
		return nil, nil
	}
	selector := func(_, profiles *jsonx.Object) ([]string, error) {
		var ids []string
		for _, profileID := range profiles.Keys() {
			if profileID == "other" {
				ids = append(ids, profileID)
			}
		}
		return ids, nil
	}
	shared, profiles, err := r.UpdateSharedAndProfilesTransaction(bg, add, compose, selector)
	noError(t, err)
	if len(seen) != 1 {
		t.Fatalf("seen %v", seen)
	}
	wantEqual(t, seen["other"][0], []any{"lib"})
	wantEqual(t, seen["other"][1], []any{"lib", "new"})
	wantEqual(t, jsonx.ListOf(profiles.Keys()), []any{"other"})
	wantEqual(t, shared.Value("_revision"), sharedRevision+1)
	wantEqual(t, must(r.GetProfile(bg, "other"))(t).Value("_revision"), revisions["other"]+1)
	wantEqual(t, must(r.GetProfile(bg, "default"))(t).Value("_revision"), revisions["default"])
	stored := rcAt(t, must(r.GetProfile(bg, "other"))(t).Value("rule_configs"), 0)
	wantEqual(t, stored, parse(t, `{"id": "ref", "itemType": "ruleset", "library_rule_id": "new", "policy": "DIRECT"}`))
	resolved := rcAt(t, must(r.GetCompatConfig(bg, "other", nil))(t).Value("rule_configs"), 0)
	wantEqual(t, resolved.Value("behavior"), "classical")
	if !strings.HasSuffix(resolved.GetString("url"), "/rule-library/content/new") {
		t.Fatalf("url %q", resolved.GetString("url"))
	}
}

func TestSharedAndProfilesTransactionRollsBackAll(t *testing.T) {
	r := rcRuleDNSRepository(t)
	before := must(r.ExportAll(bg, false))(t)
	rename := rcUpd(func(shared *jsonx.Object) { rcAt(t, shared.Value("rule_library"), 0).Set("name", "Renamed") })
	fail := func(_ context.Context, profileID string, _, after *jsonx.Object) (*jsonx.Object, error) {
		after.Set("rule_configs", []any{})
		if profileID == "other" {
			return nil, NewValidationError("boom")
		}
		return nil, nil
	}
	_, _, err := r.UpdateSharedAndProfilesTransaction(bg, rename, fail, Profiles("default", "other"))
	wantError(t, err, ErrValidation, "boom")
	wantEqual(t, must(r.ExportAll(bg, false))(t), before)
	// Whole-document validation also rejects everything at once.
	follow := func(_ context.Context, _ string, _, after *jsonx.Object) (*jsonx.Object, error) {
		after.GetObject("oxidns").Set("rule_dns_follow", []any{"missing"})
		return nil, nil
	}
	_, _, err = r.UpdateSharedAndProfilesTransaction(bg, rename, follow, Profiles("default"))
	wantError(t, err, ErrInUse, "")
	wantEqual(t, must(r.ExportAll(bg, false))(t), before)
}

func TestRuleDnsIdsMustExist(t *testing.T) {
	r := rcRuleDNSRepository(t)
	_, _, err := r.UpdateProfileTransaction(bg, "default", rcOxidnsUpdate(parse(t, `{
		"rule_actions": {"set": {"action": "blackhole"}}, "rule_dns_follow": ["rule"],
		"rule_dns_parked": {"rule": {"action": "direct"}}}`)))
	noError(t, err)
	for _, fields := range []string{
		`{"rule_actions": {"gone": {"action": "refused"}}}`,
		`{"rule_dns_follow": ["gone"]}`,
		`{"rule_dns_parked": {"gone": {"action": "proxy"}}}`,
	} {
		_, _, err := r.UpdateProfileTransaction(bg, "default", rcOxidnsUpdate(parse(t, fields)))
		wantError(t, err, ErrInUse, "OxiDNS DNS 处理仍引用不存在的规则 gone")
	}
	_, _, err = r.UpdateProfileTransaction(bg, "default", rcOxidnsUpdate(parse(t, `{"rule_actions": "x"}`)))
	wantError(t, err, ErrValidation, "")
}

func TestRuleDnsActionShapesAreValidated(t *testing.T) {
	r := rcRuleDNSRepository(t)
	for _, entry := range [][2]string{
		{"rule_actions", `{"set": {"kind": "blackhole"}}`},
		{"rule_actions", `{"set": "reject"}`},
		{"rule_actions", `{"set": {"action": "direct"}}`},
		{"rule_actions", `{"set": {"action": "upstream", "upstream": {"addr": "1.1.1.1"}}}`},
		{"rule_dns_parked", `{"rule": {"action": "drop"}}`},
	} {
		fields := parse(t, `{"`+entry[0]+`": `+entry[1]+`}`)
		_, _, err := r.UpdateProfileTransaction(bg, "default", rcOxidnsUpdate(fields))
		wantError(t, err, ErrValidation, "OxiDNS "+entry[0]+" 中规则")
	}
	// Imports skip the settings normalizer and must be refused the same way.
	exported := must(r.ExportProfile(bg, "default"))(t)
	exported.GetObject("oxidns").Set("rule_actions", parse(t, `{"set": {"kind": "blackhole"}}`))
	_, err := r.ImportProfile(bg, "default", exported)
	wantError(t, err, ErrValidation, "")
	wantEqual(t, must(r.GetProfile(bg, "default"))(t).GetObject("oxidns").GetOr("rule_actions", jsonx.NewObject()), jsonx.NewObject())
}

// The route half of test_every_delete_path_cleans_rule_dns_fields (DELETE
// /api/rules/<id>) belongs to the rules routes package; this is the import
// half.
func TestEveryDeletePathCleansRuleDnsFields(t *testing.T) {
	r := rcRuleDNSRepository(t)
	fields := parse(t, `{"rule_actions": {"set": {"action": "blackhole"}}, "rule_dns_follow": ["rule"], "rule_dns_parked": {}}`)
	for _, profileID := range []string{"default", "other"} {
		_, _, err := r.UpdateProfileTransaction(bg, profileID, rcOxidnsUpdate(fields))
		noError(t, err)
	}
	// An import replacing the composition drops the DNS entries of rules it lacks.
	exported := must(r.ExportProfile(bg, "default"))(t)
	kept := []any{}
	for _, item := range exported.GetList("rule_configs") {
		if item.(*jsonx.Object).Value("id") != "set" {
			kept = append(kept, item)
		}
	}
	exported.Set("rule_configs", kept)
	exported.Delete("oxidns")
	must(r.ImportProfile(bg, "default", exported))(t)
	oxidns := must(r.GetProfile(bg, "default"))(t).GetObject("oxidns")
	wantEqual(t, oxidns.Value("rule_actions"), jsonx.NewObject())
	wantEqual(t, oxidns.Value("rule_dns_follow"), []any{"rule"})
}
