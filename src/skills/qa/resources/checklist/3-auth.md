<!-- prereq: 0.3 -->

## 3. Authentication (`forge authentication`)

Tests credential storage/resolution in `$FORGE_HOME/credentials.yaml` (named profiles).

> **Note:** These steps test Forge's credential management UI only. The keys stored via `forge authentication login` are
> NOT used by the proxy or Claude Code. The proxy gets its backend keys from environment variables injected at container
> start (`/etc/profile.d/forge-qa.sh`). You can use placeholder values (e.g., `sk-ant-manual-test-12345`) for all login
> prompts.

### 3.1 Login — Store Credentials

<!-- human:guided -->

In a **shell inside the QA container** (`docker exec -it $CONTAINER bash -l` — the container name is printed by
`start-container.sh`, usually `forge-qa`), run the command below. Enter a test API key when prompted (e.g.,
`sk-ant-manual-test-12345`). Input will be hidden.

```
# Store credentials for a single credential
forge authentication login -c anthropic-api

# Expected: prompts for ANTHROPIC_API_KEY (input hidden)
# Enter a test key, e.g.: sk-ant-manual-test-12345
```

- [ ] Prompts for API key with hidden input (characters not echoed)
- [ ] Shows "Credentials saved to $FORGE_HOME/credentials.yaml"
- [ ] File created with 0o600 permissions (`ls -la $FORGE_HOME/credentials.yaml`)

### 3.2 Login — Named Profiles

<!-- human:guided -->

In the **container shell**, store credentials under a named profile. Enter a different test key (e.g.,
`sk-ant-work-key-99999`).

```
# Store credentials in a named profile
forge authentication login -c anthropic-api --profile work
# Enter a different key, e.g.: sk-ant-work-key-99999

# Verify both profiles exist
forge authentication profiles
```

- [ ] `work` profile created separately from `default`
- [ ] `forge authentication profiles` shows both profiles with key counts
- [ ] Active profile marked with "← active"

### 3.3 Login — Keep Existing Values

<!-- human:guided -->

In the **container shell**, re-run login for the same credential. The existing value appears as a masked default (e.g.,
`ANTHROPIC_API_KEY [sk-a…5678]`). Press Enter to keep it.

```
# Re-run login for same credential — existing value shown as masked default
forge authentication login -c anthropic-api

# Expected: shows existing value like "ANTHROPIC_API_KEY [sk-a…5678]"
# Press Enter to keep existing value
```

- [ ] Existing value shown as masked default (first 4 + last 4 chars)
- [ ] Pressing Enter preserves the existing value (not overwritten)

### 3.4 Status — Dual-View Output

<!-- auto -->

```bash
# Check credential status
forge authentication status

# Expected output has two sections:
#   Configured capabilities:
#     * anthropic-api ...  (file:default)
#
#   Credential details:
#     anthropic-api
#       * ANTHROPIC_API_KEY = sk-a…5678  (file:default)
```

- [ ] Shows "Configured capabilities:" section with configured credentials
- [ ] Shows "Not configured (set up if needed):" section for unconfigured credentials
- [ ] Shows "Credential details:" section with per-variable source attribution
- [ ] Values are masked (never shown in full)
- [ ] All 5 credentials displayed (openrouter, anthropic-api, openai-api, gemini-api, litellm-remote)
- [ ] Unconfigured credentials show "not configured" (not "MISSING")

### 3.5 Status — Env Overrides File

<!-- auto -->

```bash
# Set env var that also exists in file
export ANTHROPIC_API_KEY=sk-ant-from-env-override
forge authentication status

# Expected: shows (env) source, not (file:default)
unset ANTHROPIC_API_KEY
```

- [ ] Env var takes precedence — shown as `(env)` not `(file:default)`
- [ ] After unsetting env var, status shows `(file:default)` again

### 3.6 Status — Named Profile

<!-- auto -->

```bash
# Check status for a specific profile
forge authentication status --profile work
```

- [ ] Shows `(file:work)` for keys stored in work profile
- [ ] Keys not in work profile shown as "not configured" (even if in default)

### 3.7 Profiles — List and Active Marker

<!-- auto -->

```bash
# List all profiles
forge authentication profiles

# Change active profile via env var
FORGE_PROFILE=work forge authentication profiles
```

- [ ] Shows profile names with key counts
- [ ] Default profile marked "← active" by default
- [ ] `FORGE_PROFILE=work` changes active marker to work

### 3.8 Logout — Remove Profile

<!-- auto -->

```bash
# Remove a profile (with confirmation)
printf 'y\n' | forge authentication logout --profile work

# Verify removed
forge authentication profiles
```

- [ ] Confirmation prompt shown (unless `-y` used)
- [ ] Profile removed from credentials file
- [ ] Other profiles unaffected

### 3.9 Logout — Skip Confirmation

<!-- human:guided -->

In the **container shell**, create a temp profile (enter any test key when prompted), then remove it with `-y`.

```
# Re-create and remove without confirmation
forge authentication login -c anthropic-api --profile temp
# Enter any test key

forge authentication logout --profile temp -y
forge authentication profiles
```

- [ ] `-y` flag skips confirmation
- [ ] Profile removed immediately

### 3.10 Credential File Security

<!-- auto -->

```bash
# Check file permissions
ls -la $FORGE_HOME/credentials.yaml
# Expected: -rw------- (0o600)

# Check file content structure without exposing secrets
python3 -c "
import yaml, sys
with open('$FORGE_HOME/credentials.yaml') as f:
    data = yaml.safe_load(f)
print('version:', data.get('version'))
print('profiles:', list(data.get('profiles', {}).keys()))
for name, profile in data.get('profiles', {}).items():
    masked = {k: v[:6] + '...' if isinstance(v, str) and len(v) > 6 else v for k, v in profile.items()}
    print(f'  {name}: {masked}')
"
```

- [ ] Permissions are 0o600 (owner read/write only)
- [ ] File has `version: 1` field
- [ ] Profile names map to flat key-value pairs (values masked)

### 3.11 Credential Resolution in CredentialManager

<!-- auto -->

```bash
# Verify file-based credentials are actually used by the system
# Unset env var, rely on file-based credential
unset ANTHROPIC_API_KEY

# If you have a valid key stored via forge authentication login:
# Starting a session or proxy that needs ANTHROPIC_API_KEY should work
# without the env var set (it reads from $FORGE_HOME/credentials.yaml)
forge authentication status --profile default
# Should show: * ANTHROPIC_API_KEY = sk-a…xxxx  (file:default)
```

- [ ] Credential available via file when env var is unset
- [ ] `forge authentication status` confirms file source

### 3.12 Retired Credential Names

<!-- auto -->

```bash
# Old 'anthropic' name should produce migration guidance
forge authentication login -c anthropic 2>&1 || true
# Expected: exit 1, yellow message mentioning 'anthropic-api'

# Old 'litellm-local' name should explain it's not a credential
forge authentication login -c litellm-local 2>&1 || true
# Expected: exit 1, message mentioning gemini-api, openai-api, anthropic-api
```

- [ ] `anthropic` produces clear migration message pointing to `anthropic-api`
- [ ] `litellm-local` explains it's not a credential and lists provider alternatives

---
