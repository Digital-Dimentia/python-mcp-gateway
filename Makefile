SHELL := /bin/bash

# --- environment contract -------------------------------------------------
# VENV_DIR is pinned, not inferred: every developer and every CI leg uses the same
# directory unless they deliberately override it.
# PYTHON is the interpreter the venv is *built from*. If it resolves to an interpreter
# inside a virtual environment (an activated shell), the bootstrap steps out to its base
# interpreter first.
PYTHON ?= python3
VENV_DIR ?= .venv
PYTHON_BIN := $(VENV_DIR)/bin/python
VENV_STAMP := $(VENV_DIR)/.mcp-gateway-venv.json
VENV_BOOTSTRAP := scripts/venv_bootstrap.py

# The directory holding this Makefile, resolved absolutely. `run` and `connect` are
# launched by *other programs* -- a supervisor starting the daemon, an MCP client
# spawning the bridge -- which pick their own cwd and may reach this file as
# `make -f /path/to/python-mcp-gateway/Makefile run`. `make -C` chdirs; `-f` does not, so
# every relative path here (`scripts/`, `.venv/`) would otherwise resolve against the
# caller's directory. See ENSURE_VENV and the launch recipes.
MAKEFILE_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# Spelled once because the launch targets run the same step themselves; two copies of
# this line would drift.
VENV_BOOTSTRAP_CMD = $(PYTHON) $(VENV_BOOTSTRAP) --venv-dir $(VENV_DIR) --python $(PYTHON) $(VENV_FLAGS)

# What `run` and `connect` use *instead of* a `venv` prerequisite. make resolves a
# prerequisite against its own cwd, so `make -f /path/to/Makefile run` from elsewhere
# dies on `No rule to make target 'pyproject.toml'` before a single recipe line runs, and
# no amount of chdir'ing inside the recipe can help. The other targets keep the
# prerequisite: nothing launches `make test`.
ENSURE_VENV = cd '$(MAKEFILE_DIR)' && $(VENV_BOOTSTRAP_CMD)

BUILD_DIR := dist
ARTIFACTS_DIR := artifacts
CONTAINER_SCRIPT := scripts/container_image.py
CONTAINER_TAG ?= python-mcp-gateway:local

# REQUIRE_CONTAINER=1 makes `container-image` fail rather than skip when no usable engine
# is present. Empty by default so packaging works on a machine without one; set in
# .github/workflows/publish-artifacts.yml.
REQUIRE_CONTAINER ?=

# PLATFORMS is empty by default: build for the host, which is what a developer iterating
# locally wants. Two or more entries produce a manifest list and need QEMU for any
# platform that is not the host's, so the release workflow sets it rather than every
# local build paying emulation cost. linux/arm64 covers Raspberry Pi on 64-bit Pi OS.
PLATFORMS ?=
RELEASE_PLATFORMS := linux/amd64,linux/arm64

CONTAINER_FLAGS := $(if $(strip $(REQUIRE_CONTAINER)),--require,) \
	$(if $(strip $(PLATFORMS)),--platform $(strip $(PLATFORMS)),)

START := scripts/start-gateway.sh
HOST ?= 127.0.0.1
# 8765 is the CLI's own default and the port the README, the container examples and
# transport_ws.md all advertise.
PORT ?= 8765
CONFIG ?= servers.yaml

# The URL `make connect` dials. Matches HOST/PORT by default so the two targets agree
# without anyone having to notice they should.
URL ?= ws://$(HOST):$(PORT)/mcp

# LOG is optional and off by default. LOG=1 takes the start script's default path
# (logs/mcp-gateway.log); any other value is used as the path itself.
LOG ?=
RUN_LOG_FLAG := $(if $(strip $(LOG)),$(if $(filter 1,$(strip $(LOG))),--log,--log=$(strip $(LOG))),)

DEBUG ?=
DEBUG_FLAG := $(if $(strip $(DEBUG)),--debug,)

# NO_KEY=1 runs `run` with no access key at all. The default is to reuse WS_ACCESS_KEY
# from gateway.env, or mint a fresh one, so the URL the banner prints is a complete,
# working example rather than something to be edited before it can be pasted. On loopback
# a key is optional -- the daemon only *refuses* a keyless bind off loopback -- but it
# costs nothing and it stops another local account from opening a connection, which would
# hand them every credential in gateway.env by way of the backends that hold them.
NO_KEY ?=

## `run-dev`'s own default for NO_KEY. Separate so `make run-dev DEV_NO_KEY=` can ask for
## a key without the recursive invocation overriding whatever was passed on the command line.
DEV_NO_KEY ?= 1

# Opt-in escape hatch for a TLS-intercepting proxy whose CA pip does not trust. Empty by
# default, so nothing is exported and no verification is relaxed on a normal machine.
PIP_TRUSTED_HOST ?=
ifneq ($(strip $(PIP_TRUSTED_HOST)),)
export PIP_TRUSTED_HOST
endif

# OFFLINE=1 forbids the bootstrap from touching the network; it succeeds only if the venv
# already satisfies pyproject.toml.
OFFLINE ?=
VENV_FLAGS := $(if $(strip $(OFFLINE)),--offline,)

.PHONY: venv sync install lint docs-check test ui-deps check build wheel sdist container-image \
        print-release-platforms package run run-dev connect clean clean-outputs clean-venv distclean

venv: $(VENV_STAMP)

# The stamp records the interpreter and the pyproject.toml digest the venv was built for.
# It is why `make test` does not run `pip install` -- and therefore does not need the
# network -- on every invocation.
$(VENV_STAMP): pyproject.toml $(VENV_BOOTSTRAP)
	$(VENV_BOOTSTRAP_CMD)

# Force a dependency install even when the stamp is current (dependencies changed outside
# pyproject.toml, or a half-finished install needs repairing).
sync:
	$(PYTHON) $(VENV_BOOTSTRAP) --venv-dir $(VENV_DIR) --python $(PYTHON) --sync

install: venv

lint: venv
	$(PYTHON_BIN) -m ruff check src tests scripts

## Documentation invariants nothing else enforces: relative links resolve, every Mermaid
## flowchart edge names a node its own block defines, and every production module has a
## sibling .md. See scripts/check_docs.py for what it deliberately does not check.
docs-check: venv
	$(PYTHON_BIN) scripts/check_docs.py

## The whole suite, JavaScript included. tests/test_webui_js.py runs `node --test` over
## tests/ui/ and *skips* when node or jsdom is absent, so this works on a checkout that has
## never seen npm; `make ui-deps` is what turns that skip into a run.
test: venv
	$(PYTHON_BIN) -m pytest tests

## jsdom, for the UI suite. Dev-only and deliberately not a prerequisite of `test`: the
## daemon ships no Node anything, and a green `make test` must not depend on a second
## language's package manager being reachable. See src/mcp_gateway/webui.md.
ui-deps:
	npm install --prefix tests/ui

## Validate servers.yaml and gateway.env without binding a port or spawning a backend.
## Reports every problem it finds rather than the first, and exits 2 if there is one --
## the pre-flight to run before `make run` on a machine whose config you did not write.
check: venv
	$(PYTHON_BIN) -m mcp_gateway.cli --check --config $(CONFIG)

build: venv
	mkdir -p $(BUILD_DIR)
	$(PYTHON_BIN) -m build

wheel: build
	@ls -1 $(BUILD_DIR)/*.whl 2>/dev/null | head -n 1

sdist: build
	@ls -1 $(BUILD_DIR)/*.tar.gz 2>/dev/null | head -n 1

## Build the container image and export it to $(BUILD_DIR). Skips -- exit 0, loud message
## -- when no engine is installed OR when one is installed but its backend is unreachable
## (a stopped `podman machine`, a dead docker daemon). REQUIRE_CONTAINER=1 turns any skip
## into a failure; the release workflow sets it, because a release that ships without its
## image should not be quiet about it.
container-image: venv
	$(PYTHON_BIN) $(CONTAINER_SCRIPT) \
		--tag $(CONTAINER_TAG) \
		--containerfile Containerfile \
		--context . \
		--output $(BUILD_DIR)/python-mcp-gateway-container.tar \
		$(CONTAINER_FLAGS)

## The platform list releases build for. publish-artifacts.yml reads it from here rather
## than repeating it, so the workflow and RELEASE_PLATFORMS cannot drift.
print-release-platforms:
	@printf '%s\n' '$(RELEASE_PLATFORMS)'

package: build container-image
	mkdir -p $(ARTIFACTS_DIR)
	tar -czf $(ARTIFACTS_DIR)/python-mcp-gateway-artifacts.tar.gz -C $(BUILD_DIR) .
	@echo "Generated artifacts:"
	@ls -l $(BUILD_DIR) $(ARTIFACTS_DIR)

## Start the daemon. Goes through $(START) rather than calling the interpreter directly,
## because the script *activates* the venv instead of merely running its python. That
## difference is invisible here and load-bearing one level down: a backend that
## servers.yaml names as a bare `python` or `npx` inherits PATH from this process.
##
## The key is **exported, never passed as an argument**: argv is world-readable through
## `ps`, so a --ws-key flag would publish the secret to every other user of the machine at
## the moment it is meant to protect it. An exported empty string reads as "no key",
## which is exactly what access_key_from_env() does with it.
##
## The whole body is one shell command, so the leading `cd $(MAKEFILE_DIR)` covers every
## line of it.
run:
	@$(ENSURE_VENV)
	@cd '$(MAKEFILE_DIR)' || exit 1; \
	key=""; \
	if [ -n "$${MCP_GATEWAY_WS_KEY:-}" ]; then \
		key="$$MCP_GATEWAY_WS_KEY"; \
	elif [ -z "$(strip $(NO_KEY))" ]; then \
		key=$$($(PYTHON_BIN) -c 'import secrets; print(secrets.token_urlsafe(32))'); \
	fi; \
	printf 'Starting mcp-gateway$(if $(strip $(DEBUG)), (debug),)...\n' >&2; \
	if [ -n "$$key" ]; then \
		: 'Percent-encode for the URL. A generated key is already URL-safe base64,'; \
		: 'but one supplied through the environment need not be, and a raw & or space'; \
		: 'would make the banner print a URL that quietly does not work. Handed over in'; \
		: 'the environment, not argv, for the reason above.'; \
		enc=$$(MCPGW_RAW_KEY="$$key" $(PYTHON_BIN) -c \
			'import os, urllib.parse; print(urllib.parse.quote(os.environ["MCPGW_RAW_KEY"], safe=""))'); \
		printf 'MCP endpoint:   ws://$(HOST):$(PORT)/mcp?key=%s\n' "$$enc" >&2; \
		printf 'Admin endpoint: ws://$(HOST):$(PORT)/admin?key=%s\n' "$$enc" >&2; \
		printf 'Admin UI:       http://$(HOST):$(PORT)/ui/?key=%s\n' "$$enc" >&2; \
		printf '\nAttach a client:\n' >&2; \
		printf '  claude mcp add gateway -- mcp-gateway-connect --url ws://$(HOST):$(PORT)/mcp?key=%s\n' "$$enc" >&2; \
	else \
		printf 'MCP endpoint:   ws://$(HOST):$(PORT)/mcp   (no key; loopback clients only)\n' >&2; \
		printf 'Admin endpoint: ws://$(HOST):$(PORT)/admin\n' >&2; \
		printf 'Admin UI:       http://$(HOST):$(PORT)/ui/\n' >&2; \
		printf '\nAttach a client:\n' >&2; \
		printf '  claude mcp add gateway -- mcp-gateway-connect --url ws://$(HOST):$(PORT)/mcp\n' >&2; \
	fi; \
	printf '\nBackends come from $(CONFIG); credentials from gateway.env beside it.\n' >&2; \
	printf 'Press Ctrl+C to stop. kill -HUP to reload config.\n\n' >&2; \
	export MCP_GATEWAY_WS_KEY="$$key"; \
	exec $(START) --host $(HOST) --port $(PORT) --config $(CONFIG) $(DEBUG_FLAG) $(RUN_LOG_FLAG)

## Start the daemon against servers.dev.yaml: the schema zoo and nothing else.
##
## For looking at the admin UI without configuring a real integration first. No access key
## by default -- the zoo has no credentials, the bind is loopback, and a key in the URL is
## one more thing between you and the page you are trying to look at. `make run-dev
## DEV_NO_KEY=` generates one anyway.
##
## A recursive $(MAKE) rather than a copy of the banner: two copies of that is how one of
## them ends up printing a URL that quietly does not work.
run-dev:
	@$(MAKE) run CONFIG=servers.dev.yaml NO_KEY=$(DEV_NO_KEY)

## Run the stdio<->WS bridge in the foreground, for reproducing a client's handshake by
## hand. **Nothing is written to stdout**: that is the protocol wire here, and one stray
## byte desynchronizes the client -- which is why the bootstrap runs with stdout folded
## onto stderr rather than as a make prerequisite (it logs to stdout, and make would run
## it before any redirection here could catch it). Each line needs its own `cd`, because
## make runs every recipe line in a fresh shell.
connect:
	@$(ENSURE_VENV) 1>&2
	@printf 'Bridging stdin/stdout to $(URL); diagnostics on stderr.\n' >&2
	@cd '$(MAKEFILE_DIR)' && exec $(PYTHON_BIN) -m mcp_gateway.bridge --url '$(URL)' $(DEBUG_FLAG)

## Build outputs and tool caches. **Leaves the virtual environment alone.** Deleting it
## is the one action here that forces a full reinstall over the network -- and behind a
## TLS-intercepting proxy that may not be recoverable at all without PIP_TRUSTED_HOST. A
## target named `clean` should not be able to leave a checkout unbuildable offline.
clean: clean-outputs
	@printf 'Build outputs and caches removed. $(VENV_DIR) was left alone;\n'
	@printf 'use `make clean-venv` to remove it (needs the network to rebuild).\n'

## The removal itself, shared with `distclean` so that target does not inherit the note
## above -- which would be false there, since `distclean` does remove the venv.
##
## `src/*.egg-info` and not just `*.egg-info`: this is a src-layout project, so the
## editable install writes `src/python_mcp_gateway.egg-info`.
clean-outputs:
	rm -rf build $(BUILD_DIR) $(ARTIFACTS_DIR) src/*.egg-info *.egg-info \
		.pytest_cache .ruff_cache

## The virtual environment, and nothing else. Honours VENV_DIR, so
## `make clean-venv VENV_DIR=.venv312` removes that one and leaves the default alone.
clean-venv:
	rm -rf $(VENV_DIR)

## Everything `clean` removes, plus the venv -- the GNU convention name. tests/ui's installed
## tree goes with it, for the same reason the venv does: both need the network to rebuild,
## which is why neither is in `clean`.
distclean: clean-outputs clean-venv
	rm -rf tests/ui/node_modules
