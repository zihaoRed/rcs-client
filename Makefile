PROFILE ?= default
PY ?= python3
PI ?= $(PI_USER)@$(PI_HOST)

.PHONY: setup init build-harvester harvester mls-sync deploy status clean

setup:
	$(PY) -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt
	cd pc/agent && npm ci && npm run build

init:
	./scripts/init_profile.sh

build-harvester:
	cd pc/agent && npm run build

harvester:
	set -a; [ -f .env ] && . ./.env; set +a; \
	$(PY) pc/token_harvester.py --profile $${PROFILE:-$(PROFILE)} --pi-push

mls-sync:
	set -a; [ -f .env ] && . ./.env; set +a; \
	$(PY) pc/mls_state.py export --profile $${PROFILE:-$(PROFILE)} --push

deploy:
	./deploy/install_pi.sh

status:
	./scripts/status.sh

clean:
	rm -rf pc/work .venv pc/frida/*.js mls/mls_probe/vendor mls/mls_probe/target
