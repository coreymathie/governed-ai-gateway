# Example OPA policy for governed-ai-gateway (OPA 1.x / Rego v1 syntax).
#
# The gateway POSTs {"input": {...}} to /v1/data/router/decision after its own
# policy allows a request. Input fields: team, key_label, alias, targets
# (["provider/model", ...] already permitted locally), max_tokens,
# request_bytes, messages, stream, required_hooks, local_rules. No message
# content is ever sent.
#
# Run:   opa run --server --addr 127.0.0.1:8181 policies/
#        OPA_URL=http://127.0.0.1:8181 uvicorn router.main:app
# Test:  opa test policies/
package router

# Teams whose data must stay on local models.
local_only_teams := {"regulated", "clinical"}

# Business hours (UTC) during which the batch alias may not run.
batch_blocked_hours := numbers.range(14, 21)

deny contains msg if {
	input.team in local_only_teams
	some t in input.targets
	not startswith(t, "ollama/")
	msg := sprintf("team %s may only use local models, route includes %s", [input.team, t])
}

deny contains msg if {
	input.alias == "cheap-batch"
	time.clock(time.now_ns())[0] in batch_blocked_hours
	msg := "cheap-batch is reserved for off-peak hours (21:00-14:00 UTC)"
}

deny contains msg if {
	input.max_tokens > 8192
	msg := sprintf("max_tokens %d is above the org-wide limit of 8192", [input.max_tokens])
}

# Streaming isn't allowed for keys labelled as batch jobs.
deny contains msg if {
	input.stream
	startswith(input.key_label, "batch-")
	msg := "batch keys may not stream"
}

# Only keep deployments outside the deny list (narrows, never widens, the route).
denied_models := {"openai/gpt-4.1"}

allowed_targets := [t | some t in input.targets; not t in denied_models]

decision := {
	"allow": count(deny) == 0,
	"reasons": sort([m | some m in deny]),
	"allowed_targets": allowed_targets,
}
