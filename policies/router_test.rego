package router_test

import data.router

base := {
	"team": "product",
	"key_label": "web-app",
	"alias": "smart-fast",
	"targets": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini"],
	"max_tokens": 512,
	"request_bytes": 120,
	"messages": 1,
	"stream": false,
	"required_hooks": [],
	"local_rules": ["defaults"],
}

test_ordinary_request_allowed if {
	d := router.decision with input as base
	d.allow
	d.reasons == []
	d.allowed_targets == ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini"]
}

test_regulated_team_denied_public_provider if {
	d := router.decision with input as object.union(base, {"team": "regulated"})
	not d.allow
	count(d.reasons) == 2
}

test_regulated_team_allowed_local if {
	d := router.decision with input as object.union(base, {"team": "regulated", "targets": ["ollama/llama3.1:8b"]})
	d.allow
}

test_max_tokens_limit if {
	d := router.decision with input as object.union(base, {"max_tokens": 9000})
	not d.allow
}

test_batch_keys_cannot_stream if {
	d := router.decision with input as object.union(base, {"stream": true, "key_label": "batch-nightly"})
	not d.allow
}

test_denied_model_removed_not_denied if {
	d := router.decision with input as object.union(base, {"targets": ["openai/gpt-4.1", "anthropic/claude-sonnet-4-5"]})
	d.allow
	d.allowed_targets == ["anthropic/claude-sonnet-4-5"]
}

test_batch_alias_blocked_in_business_hours if {
	d := router.decision with input as object.union(base, {"alias": "cheap-batch"})
		with time.now_ns as time.parse_rfc3339_ns("2026-10-05T15:00:00Z")
	not d.allow
}

test_batch_alias_allowed_off_peak if {
	d := router.decision with input as object.union(base, {"alias": "cheap-batch"})
		with time.now_ns as time.parse_rfc3339_ns("2026-10-05T23:00:00Z")
	d.allow
}
