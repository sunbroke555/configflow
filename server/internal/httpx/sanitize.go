package httpx

import (
	"configflow/internal/jsonresp"
	"configflow/internal/jsonx"
	"configflow/internal/redact"
)

// External is backend.common.json_response.jsonify_external: ordinary
// metadata encoded once, already redacted against the current stored
// credentials, so the global hook does not decode and re-encode a large
// body. The wire bytes equal those of JSON(v) after the hook. Payloads that
// are not plain JSON trees (deeper than 128 levels, cyclic, not an object
// or list) fall back to JSON(v).
func External(c *Ctx, v any) (*Response, error) {
	v = normalizeValue(v)
	if !jsonresp.PlainTree(v) {
		return JSON(v), nil
	}
	secrets, err := redact.ExternalPayloadSecrets(c.ctx, nil)
	if err != nil {
		return nil, err
	}
	sanitized := redact.SanitizeWithSecrets(v, secrets, true)
	body, err := jsonresp.External(sanitized)
	if err != nil {
		return nil, err
	}
	r := RawJSON(200, body)
	r.sanitized = jsonresp.NewSanitized(body, secrets, c.Endpoint)
	return r, nil
}

var renderDraftEndpoints = map[string]bool{
	"oxidns.render_oxidns_draft": true, "profiles.profile_render_oxidns_draft": true,
}

var oxidnsSettingsEndpoints = map[string]bool{
	"oxidns.oxidns_settings": true, "profiles.profile_oxidns_settings": true,
	"oxidns.prepare_oxidns_editor": true, "profiles.profile_prepare_oxidns_editor": true,
	"oxidns.preview_oxidns_template": true, "profiles.profile_preview_oxidns_template": true,
	"oxidns.apply_oxidns_common_settings": true, "profiles.profile_apply_oxidns_common_settings": true,
	"oxidns.apply_oxidns_native_settings": true, "profiles.profile_apply_oxidns_native_settings": true,
}

var oxidnsNativeKeys = []string{"native_config", "additional_files", "template_settings",
	"template_base_config", "legacy_native_config", "_common_disabled", "gateway_onboarding_native_backup"}

var oxidnsValuesEndpoints = map[string]bool{
	"oxidns.read_oxidns_common_settings": true, "profiles.profile_read_oxidns_common_settings": true,
	"oxidns.apply_oxidns_common_settings": true, "profiles.profile_apply_oxidns_common_settings": true,
	"oxidns.apply_oxidns_native_settings": true, "profiles.profile_apply_oxidns_native_settings": true,
}

var tokenEndpoints = map[string]bool{"agents.register_agent": true, "agents.docker_oxidns_ha_enroll": true}

var artifactEndpoints = map[string]bool{
	"agents.get_agent_config": true, "agents.generate_final_agent_config": true,
	"generate.generate_artifact": true, "profiles.profile_generate_artifact": true,
}

// maxRawJSONDepth approximates where CPython's JSON decoder hits the
// recursion limit for a raw JSON body.
const maxRawJSONDepth = 990

// sanitizeResponse is profile_context._sanitize_external_json: every JSON
// response that is not a file and was not prepared by External is decoded,
// redacted and re-encoded with the provider's dumps (sorted keys, ASCII,
// ", "/": " separators, no trailing newline), except for the endpoint
// whitelists below that must round-trip exact editor content.
func sanitizeResponse(c *Ctx, r *Response) error {
	if !r.IsJSON() || r.passthrough {
		return nil
	}
	marker := r.sanitized
	r.sanitized = nil
	if marker != nil && !r.hasValue {
		secrets, err := redact.ExternalPayloadSecrets(c.ctx, nil)
		if err != nil {
			return err
		}
		if marker.Consume(r.body, c.Endpoint, secrets) {
			return nil
		}
	}
	var payload any
	sortKeys := false
	if r.hasValue {
		payload, sortKeys = r.value, true
	} else {
		if jsonDepth(r.body) > maxRawJSONDepth {
			r.SetBody([]byte(`"[REDACTED]"`))
			return nil
		}
		text, err := decodeJSONBytes(r.body)
		if err != nil {
			return nil
		}
		if payload, err = jsonx.Parse(text); err != nil {
			return nil
		}
	}
	if payload == nil {
		return nil
	}
	secrets, err := redact.ExternalPayloadSecrets(c.ctx, nil)
	if err != nil {
		return err
	}
	sanitized := redact.SanitizeWithSecrets(payload, secrets, sortKeys)
	original, isDict := payload.(*jsonx.Object)
	target, _ := sanitized.(*jsonx.Object)
	if isDict && target != nil {
		applyWhitelists(c, r.Status, original, target)
	}
	body, err := jsonresp.External(sanitized)
	if err != nil {
		return err
	}
	r.SetBody(body)
	return nil
}

func applyWhitelists(c *Ctx, status int, payload, sanitized *jsonx.Object) {
	endpoint := c.Endpoint
	if status == 200 {
		if renderDraftEndpoints[endpoint] {
			if config, ok := payload.Value("config").(string); ok {
				sanitized.Set("config", config)
			}
		} else if oxidnsSettingsEndpoints[endpoint] {
			original, target := any(payload), any(sanitized)
			if c.Request.Method == "POST" {
				original, target = payload.Value("settings"), sanitized.Value("settings")
			}
			o, ok1 := original.(*jsonx.Object)
			t, ok2 := target.(*jsonx.Object)
			if ok1 && ok2 && o != nil && t != nil {
				for _, key := range oxidnsNativeKeys {
					if value, ok := o.Get(key); ok {
						t.Set(key, value)
					}
				}
			}
		}
		if oxidnsValuesEndpoints[endpoint] {
			if values, ok := payload.Value("values").(*jsonx.Object); ok && values != nil {
				sanitized.Set("values", values)
			}
		}
	}
	if endpoint == "settings.handle_rule_fetch_proxy" && status == 200 {
		if proxy, ok := payload.Value("rule_fetch_proxy").(string); ok {
			sanitized.Set("rule_fetch_proxy", proxy)
		}
	}
	if tokenEndpoints[endpoint] && status >= 200 && status < 300 && payload.Value("success") == true {
		if token, ok := payload.Value("token").(string); ok {
			sanitized.Set("token", token)
		}
	}
	if artifactEndpoints[endpoint] && status == 200 && payload.Value("success") == true {
		if content, ok := payload.Value("content").(string); ok {
			sanitized.Set("content", content)
		}
	}
}

// jsonDepth is the maximum container nesting of a JSON text (strings
// skipped), without decoding it.
func jsonDepth(body []byte) int {
	depth, deepest := 0, 0
	inString, escaped := false, false
	for _, b := range body {
		if inString {
			switch {
			case escaped:
				escaped = false
			case b == '\\':
				escaped = true
			case b == '"':
				inString = false
			}
			continue
		}
		switch b {
		case '"':
			inString = true
		case '[', '{':
			depth++
			if depth > deepest {
				deepest = depth
			}
		case ']', '}':
			depth--
		}
	}
	return deepest
}
