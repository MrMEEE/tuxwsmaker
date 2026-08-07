from __future__ import annotations

import json
import shlex
from pathlib import Path

from apps.afterburners.models import AfterburnerItem, AfterburnerScriptInput


RHSM_REPO_IDS_FILENAME = "rhsm-repositories.txt"


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _build_item_snippet(item: AfterburnerItem) -> str:
    cfg = dict(item.config or {})
    label = item.name or item.get_item_type_display()

    if item.item_type == AfterburnerItem.TYPE_HOSTNAME:
        return (
            f'echo "[afterburner] {label}: hostname"\n'
            "prompt_text HOSTNAME_VALUE \"Hostname (short name)\" \"\"\n"
            "if [[ -n \"${HOSTNAME_VALUE:-}\" ]]; then\n"
            "  prompt_text HOSTNAME_DOMAIN \"Domain name (optional)\" \"\"\n"
            "  FQDN_VALUE=\"$HOSTNAME_VALUE\"\n"
            "  if [[ -n \"${HOSTNAME_DOMAIN:-}\" ]]; then\n"
            "    FQDN_VALUE=\"$HOSTNAME_VALUE.$HOSTNAME_DOMAIN\"\n"
            "  fi\n"
            "  run_chroot hostnamectl set-hostname \"$FQDN_VALUE\"\n"
            "  echo \"$FQDN_VALUE\" > \"$TARGET_ROOT/etc/hostname\"\n"
            "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_LOCAL_USER:
        return (
            f'echo "[afterburner] {label}: local user"\n'
            "prompt_text LOCAL_USER_USERNAME \"Username (blank to skip)\" \"\"\n"
            "if [[ -n \"${LOCAL_USER_USERNAME:-}\" ]]; then\n"
            "  prompt_text LOCAL_USER_FIRSTNAME \"First name\" \"\"\n"
            "  prompt_text LOCAL_USER_LASTNAME \"Last name\" \"\"\n"
            "  while true; do\n"
            "    prompt_password LOCAL_USER_PASS_1 \"Password\"\n"
            "    prompt_password LOCAL_USER_PASS_2 \"Confirm password\"\n"
            "    if [[ \"${LOCAL_USER_PASS_1:-}\" != \"${LOCAL_USER_PASS_2:-}\" ]]; then\n"
            "      echo \"Passwords do not match. Try again.\" >&2\n"
            "      continue\n"
            "    fi\n"
            "    if [[ -z \"${LOCAL_USER_PASS_1:-}\" ]]; then\n"
            "      echo \"Password cannot be empty.\" >&2\n"
            "      continue\n"
            "    fi\n"
            "    break\n"
            "  done\n"
            "  prompt_bool LOCAL_USER_ADMIN \"Grant sudo (wheel) access\" \"no\"\n"
            "  if ! run_chroot id \"$LOCAL_USER_USERNAME\" >/dev/null 2>&1; then\n"
            "    run_chroot useradd -m \"$LOCAL_USER_USERNAME\"\n"
            "  fi\n"
            "  FULL_NAME=\"$LOCAL_USER_FIRSTNAME $LOCAL_USER_LASTNAME\"\n"
            "  FULL_NAME=\"${FULL_NAME## }\"\n"
            "  FULL_NAME=\"${FULL_NAME%% }\"\n"
            "  if [[ -n \"${FULL_NAME:-}\" ]]; then\n"
            "    run_chroot usermod -c \"$FULL_NAME\" \"$LOCAL_USER_USERNAME\" || true\n"
            "  fi\n"
            "  printf '%s:%s\\n' \"$LOCAL_USER_USERNAME\" \"$LOCAL_USER_PASS_1\" | run_chroot chpasswd\n"
            "  if [[ \"$LOCAL_USER_ADMIN\" == \"yes\" ]]; then\n"
            "    run_chroot usermod -aG wheel \"$LOCAL_USER_USERNAME\"\n"
            "  else\n"
            "    run_chroot gpasswd -d \"$LOCAL_USER_USERNAME\" wheel >/dev/null 2>&1 || true\n"
            "  fi\n"
            "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_AD_JOIN:
        default_domain = str(cfg.get("domain") or "")
        default_ou = str(cfg.get("computer_ou") or "")
        default_join_user = str(cfg.get("join_user") or "")
        return (
            f'echo "[afterburner] {label}: AD join"\n'
            f'prompt_text AD_DOMAIN "Active Directory domain (blank to skip)" {_shell_quote(default_domain)}\n'
            "if [[ -n \"${AD_DOMAIN:-}\" ]]; then\n"
            f'  prompt_text AD_JOIN_USER "AD join username" {_shell_quote(default_join_user)}\n'
            "  prompt_password AD_JOIN_PASS \"AD join password\"\n"
            f'  prompt_text AD_COMPUTER_OU "Computer OU (optional)" {_shell_quote(default_ou)}\n'
            "  if [[ -z \"${AD_JOIN_USER:-}\" || -z \"${AD_JOIN_PASS:-}\" ]]; then\n"
            "    echo \"AD join requires user and password\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  if ! run_chroot command -v realm >/dev/null 2>&1; then\n"
            "    echo \"realm command not found in target OS\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  REALM_ARGS=(join -U \"$AD_JOIN_USER\")\n"
            "  if [[ -n \"${AD_COMPUTER_OU:-}\" ]]; then\n"
            "    REALM_ARGS+=(--computer-ou \"$AD_COMPUTER_OU\")\n"
            "  fi\n"
            "  REALM_ARGS+=(\"$AD_DOMAIN\")\n"
            "  printf '%s\\n' \"$AD_JOIN_PASS\" | run_chroot realm \"${REALM_ARGS[@]}\"\n"
            "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_STATIC_IP:
        default_iface = str(cfg.get("interface") or "")
        default_ip = str(cfg.get("ip_address") or "")
        default_prefix = str(cfg.get("prefix") or "24")
        default_gw = str(cfg.get("gateway") or "")
        default_dns = str(cfg.get("dns") or "")
        return (
            f'echo "[afterburner] {label}: static ip"\n'
            f'prompt_text STATIC_IFACE "Interface (blank to skip)" {_shell_quote(default_iface)}\n'
            "if [[ -n \"${STATIC_IFACE:-}\" ]]; then\n"
            f'  prompt_text STATIC_IP "IPv4 address" {_shell_quote(default_ip)}\n'
            f'  prompt_text STATIC_PREFIX "Prefix" {_shell_quote(default_prefix)}\n'
            f'  prompt_text STATIC_GW "Gateway" {_shell_quote(default_gw)}\n'
            f'  prompt_text STATIC_DNS "DNS (comma separated)" {_shell_quote(default_dns)}\n'
            "  if [[ -z \"${STATIC_IP:-}\" ]]; then\n"
            "    echo \"Static IP requires an address\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  if ! [[ \"$STATIC_PREFIX\" =~ ^[0-9]+$ ]] || (( STATIC_PREFIX < 1 || STATIC_PREFIX > 32 )); then\n"
            "    echo \"Prefix must be between 1 and 32\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  run_chroot nmcli con show \"$STATIC_IFACE\" >/dev/null 2>&1 || run_chroot nmcli con add type ethernet ifname \"$STATIC_IFACE\" con-name \"$STATIC_IFACE\"\n"
            "  run_chroot nmcli con mod \"$STATIC_IFACE\" ipv4.method manual ipv4.addresses \"$STATIC_IP/$STATIC_PREFIX\"\n"
            "  if [[ -n \"${STATIC_GW:-}\" ]]; then\n"
            "    run_chroot nmcli con mod \"$STATIC_IFACE\" ipv4.gateway \"$STATIC_GW\"\n"
            "  fi\n"
            "  if [[ -n \"${STATIC_DNS:-}\" ]]; then\n"
            "    run_chroot nmcli con mod \"$STATIC_IFACE\" ipv4.dns \"$STATIC_DNS\"\n"
            "  fi\n"
            "  run_chroot nmcli con up \"$STATIC_IFACE\" || true\n"
            "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_LUKS_ROTATE:
        default_device = str(cfg.get("device") or "")
        autodetect = bool(cfg.get("autodetect") or False)
        return (
            f'echo "[afterburner] {label}: rotate LUKS password"\n'
            "rotate_luks_container() {\n"
            "  local luks_dev=\"$1\"\n"
            "  echo \"[afterburner] Rotating LUKS password for $luks_dev\"\n"
            "  prompt_password LUKS_OLD \"Current LUKS password for $luks_dev\"\n"
            "  while true; do\n"
            "    prompt_password LUKS_NEW \"New LUKS password for $luks_dev\"\n"
            "    prompt_password LUKS_NEW_CONFIRM \"Confirm new LUKS password for $luks_dev\"\n"
            "    if [[ -z \"${LUKS_NEW:-}\" ]]; then\n"
            "      echo \"New LUKS password cannot be empty\" >&2\n"
            "      continue\n"
            "    fi\n"
            "    if [[ \"$LUKS_NEW\" != \"$LUKS_NEW_CONFIRM\" ]]; then\n"
            "      echo \"New LUKS passwords do not match\" >&2\n"
            "      continue\n"
            "    fi\n"
            "    break\n"
            "  done\n"
            "  if [[ -z \"${LUKS_OLD:-}\" ]]; then\n"
            "    echo \"Current LUKS password is required\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  printf '%s\\n%s\\n' \"$LUKS_OLD\" \"$LUKS_NEW\" | cryptsetup luksAddKey \"$luks_dev\" -\n"
            "  printf '%s\\n' \"$LUKS_OLD\" | cryptsetup luksRemoveKey \"$luks_dev\" -\n"
            "}\n"
            "discover_luks_devices() {\n"
            "  lsblk -rpn -o NAME,TYPE | awk '$2 == \"disk\" || $2 == \"part\" {print $1}' | while read -r candidate; do\n"
            "    [[ -n \"$candidate\" ]] || continue\n"
            "    if cryptsetup isLuks \"$candidate\" >/dev/null 2>&1; then\n"
            "      printf '%s\\n' \"$candidate\"\n"
            "    fi\n"
            "  done\n"
            "}\n"
            "declare -a LUKS_TARGETS=()\n"
            "declare -A LUKS_SEEN=()\n"
            "add_luks_target() {\n"
            "  local luks_dev=\"$1\"\n"
            "  [[ -n \"$luks_dev\" ]] || return 0\n"
            "  if [[ -z \"${LUKS_SEEN[$luks_dev]:-}\" ]]; then\n"
            "    LUKS_SEEN[$luks_dev]=1\n"
            "    LUKS_TARGETS+=(\"$luks_dev\")\n"
            "  fi\n"
            "}\n"
            f'add_luks_target {_shell_quote(default_device)}\n'
            + (
                "while IFS= read -r luks_dev; do\n"
                "  add_luks_target \"$luks_dev\"\n"
                "done < <(discover_luks_devices)\n"
                if autodetect
                else ""
            )
            + "if [[ ${#LUKS_TARGETS[@]} -eq 0 ]]; then\n"
            + "  echo \"No LUKS containers found; skipping.\"\n"
            + "else\n"
            + "  for LUKS_DEV in \"${LUKS_TARGETS[@]}\"; do\n"
            + "    rotate_luks_container \"$LUKS_DEV\"\n"
            + "  done\n"
            + "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_BOOTLOADER_PASSWORD:
        default_grub_user = str(cfg.get("grub_user") or "")
        return (
            f'echo "[afterburner] {label}: set bootloader password"\n'
            f'prompt_text GRUB_USER "GRUB superuser" {_shell_quote(default_grub_user)}\n'
            'if [[ -n "${GRUB_USER:-}" ]]; then\n'
            '  while true; do\n'
            '    prompt_password GRUB_PW_1 "GRUB password"\n'
            '    prompt_password GRUB_PW_2 "Confirm GRUB password"\n'
            '    if [[ -z "${GRUB_PW_1:-}" ]]; then\n'
            '      echo "GRUB password cannot be empty" >&2\n'
            '      continue\n'
            '    fi\n'
            '    if [[ "$GRUB_PW_1" != "$GRUB_PW_2" ]]; then\n'
            '      echo "GRUB passwords do not match" >&2\n'
            '      continue\n'
            '    fi\n'
            '    break\n'
            '  done\n'
            '  GRUB_MKPASSWD_BIN=""\n'
            '  if run_chroot command -v grub2-mkpasswd-pbkdf2 >/dev/null 2>&1; then\n'
            '    GRUB_MKPASSWD_BIN="grub2-mkpasswd-pbkdf2"\n'
            '  elif run_chroot command -v grub-mkpasswd-pbkdf2 >/dev/null 2>&1; then\n'
            '    GRUB_MKPASSWD_BIN="grub-mkpasswd-pbkdf2"\n'
            '  else\n'
            '    echo "No grub password hash utility found in target OS" >&2\n'
            '    exit 1\n'
            '  fi\n'
            '  GRUB_PW_HASH=$(printf "%s\\n%s\\n" "$GRUB_PW_1" "$GRUB_PW_1" | run_chroot "$GRUB_MKPASSWD_BIN" | awk \'/PBKDF2 hash/{print $NF}\')\n'
            '  if [[ -z "${GRUB_PW_HASH:-}" ]]; then\n'
            '    echo "Failed to generate GRUB password hash" >&2\n'
            '    exit 1\n'
            '  fi\n'
            '  GRUB_USER_CFG="$TARGET_ROOT/boot/grub2/user.cfg"\n'
            '  if [[ ! -d "$TARGET_ROOT/boot/grub2" && -d "$TARGET_ROOT/boot/grub" ]]; then\n'
            '    GRUB_USER_CFG="$TARGET_ROOT/boot/grub/user.cfg"\n'
            '  fi\n'
            '  cat > "$GRUB_USER_CFG" <<GRUBPW\n'
            'set superusers="$GRUB_USER"\n'
            'password_pbkdf2 $GRUB_USER $GRUB_PW_HASH\n'
            'GRUBPW\n'
            '  chmod 600 "$GRUB_USER_CFG"\n'
            'fi\n'
        )

    if item.item_type == AfterburnerItem.TYPE_REDHAT_REGISTRATION:
        default_username = str(cfg.get("username") or "")
        default_password = str(cfg.get("password") or "")
        default_org_id = str(cfg.get("org_id") or "")
        default_activation_key = str(cfg.get("activation_key") or "")
        default_repo_ids = str(cfg.get("repo_ids") or "")
        prompt_credentials = bool(cfg.get("prompt_credentials") or False)
        prompt_repositories = bool(cfg.get("prompt_repositories") or False)
        if prompt_credentials:
            credentials_block = (
                "prompt_bool RHSM_USE_ACTIVATION_KEY \"Use activation key registration\" \"no\"\n"
                "if [[ \"$RHSM_USE_ACTIVATION_KEY\" == \"yes\" ]]; then\n"
                "  prompt_text RHSM_ORG_ID \"Organization ID\" \"\"\n"
                "  prompt_text RHSM_ACTIVATION_KEY \"Activation key\" \"\"\n"
                "  if [[ -z \"${RHSM_ORG_ID:-}\" || -z \"${RHSM_ACTIVATION_KEY:-}\" ]]; then\n"
                "    echo \"Org ID and activation key are required\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "  run_chroot subscription-manager register --force --org \"$RHSM_ORG_ID\" --activationkey \"$RHSM_ACTIVATION_KEY\"\n"
                "else\n"
                "  prompt_text RHSM_USERNAME \"Red Hat username\" \"\"\n"
                "  prompt_password RHSM_PASSWORD \"Red Hat password\"\n"
                "  if [[ -z \"${RHSM_USERNAME:-}\" || -z \"${RHSM_PASSWORD:-}\" ]]; then\n"
                "    echo \"Username and password are required\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "  run_chroot subscription-manager register --force --username \"$RHSM_USERNAME\" --password \"$RHSM_PASSWORD\"\n"
                "fi\n"
            )
        elif default_org_id and default_activation_key:
            credentials_block = (
                "run_chroot subscription-manager register --force --org "
                + _shell_quote(default_org_id)
                + " --activationkey "
                + _shell_quote(default_activation_key)
                + "\n"
            )
        else:
            credentials_block = (
                "run_chroot subscription-manager register --force --username "
                + _shell_quote(default_username)
                + " --password "
                + _shell_quote(default_password)
                + "\n"
            )

        repo_assignment_block = (
            "prompt_text RHSM_REPO_IDS_USER \"Repository IDs (comma-separated; blank to skip)\" "
            + _shell_quote(default_repo_ids)
            + "\n"
            if prompt_repositories
            else "RHSM_REPO_IDS_USER=\"\"\n"
        )
        return (
            f'echo "[afterburner] {label}: red hat registration"\n'
            "if ! run_chroot command -v subscription-manager >/dev/null 2>&1; then\n"
            "  echo \"subscription-manager is not available in target OS\" >&2\n"
            "  exit 1\n"
            "fi\n"
            + credentials_block
            + repo_assignment_block
            + "RHSM_REPO_IDS_CONFIG=" + _shell_quote(default_repo_ids) + "\n"
            + "RHSM_REPO_FILE=/run/install/repo/deploy/"
            + RHSM_REPO_IDS_FILENAME
            + "\n"
            + "RHSM_REPO_IDS_FILE=\"\"\n"
            + "if [[ -f \"$RHSM_REPO_FILE\" ]]; then\n"
            + "  RHSM_REPO_IDS_FILE=\"$(tr '\\n' ',' < \"$RHSM_REPO_FILE\")\"\n"
            + "fi\n"
            + "RHSM_REPO_IDS_MERGED=\"$RHSM_REPO_IDS_CONFIG,$RHSM_REPO_IDS_FILE,${RHSM_REPO_IDS_USER:-}\"\n"
            + "if [[ -n \"${RHSM_REPO_IDS_MERGED//[[:space:],]/}\" ]]; then\n"
            + "  declare -A RHSM_REPO_SEEN=()\n"
            + "  while IFS= read -r RHSM_REPO_ID; do\n"
            + "    RHSM_REPO_ID=\"$(echo \"$RHSM_REPO_ID\" | xargs)\"\n"
            + "    [[ -n \"$RHSM_REPO_ID\" ]] || continue\n"
            + "    [[ -z \"${RHSM_REPO_SEEN[$RHSM_REPO_ID]:-}\" ]] || continue\n"
            + "    RHSM_REPO_SEEN[$RHSM_REPO_ID]=1\n"
            + "    run_chroot subscription-manager repos --enable=\"$RHSM_REPO_ID\"\n"
            + "  done < <(printf '%s\\n' \"$RHSM_REPO_IDS_MERGED\" | tr ',' '\\n')\n"
            + "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_TPM_INTEGRATION:
        default_device = str(cfg.get("device") or "")
        autodetect = bool(cfg.get("autodetect") or False)
        tpm_hash = str(cfg.get("hash") or "sha256").strip() or "sha256"
        tpm_key = str(cfg.get("key") or "ecc").strip() or "ecc"
        tpm_pcr_bank = str(cfg.get("pcr_bank") or "sha256").strip() or "sha256"

        raw_pcr_ids = cfg.get("pcr_ids")
        if isinstance(raw_pcr_ids, str):
            pcr_ids = [v.strip() for v in raw_pcr_ids.split(",") if v.strip()]
        elif isinstance(raw_pcr_ids, list):
            pcr_ids = [str(v).strip() for v in raw_pcr_ids if str(v).strip()]
        else:
            pcr_ids = []

        tpm_policy_payload = {
            "hash": tpm_hash,
            "key": tpm_key,
            "pcr_bank": tpm_pcr_bank,
        }
        if pcr_ids:
            tpm_policy_payload["pcr_ids"] = ",".join(pcr_ids)

        tpm_policy = json.dumps(tpm_policy_payload, separators=(",", ":"))

        return (
            f'echo "[afterburner] {label}: tpm integration"\n'
            'if ! command -v clevis >/dev/null 2>&1; then\n'
            '  echo "clevis is not available; skipping TPM integration" >&2\n'
            '  exit 1\n'
            'fi\n'
            'if ! command -v cryptsetup >/dev/null 2>&1; then\n'
            '  echo "cryptsetup is not available; skipping TPM integration" >&2\n'
            '  exit 1\n'
            'fi\n'
            'TPM2_POLICY=' + _shell_quote(tpm_policy) + '\n'
            "declare -a TPM_TARGETS=()\n"
            "declare -A TPM_SEEN=()\n"
            "add_tpm_target() {\n"
            "  local luks_dev=\"$1\"\n"
            "  [[ -n \"$luks_dev\" ]] || return 0\n"
            "  if [[ -z \"${TPM_SEEN[$luks_dev]:-}\" ]]; then\n"
            "    TPM_SEEN[$luks_dev]=1\n"
            "    TPM_TARGETS+=(\"$luks_dev\")\n"
            "  fi\n"
            "}\n"
            f'add_tpm_target {_shell_quote(default_device)}\n'
            + (
                "while IFS= read -r luks_dev; do\n"
                "  [[ -n \"$luks_dev\" ]] || continue\n"
                "  if cryptsetup isLuks \"$luks_dev\" >/dev/null 2>&1; then\n"
                "    add_tpm_target \"$luks_dev\"\n"
                "  fi\n"
                "done < <(lsblk -rpn -o NAME,TYPE | awk '$2 == \"disk\" || $2 == \"part\" {print $1}')\n"
                if autodetect
                else ""
            )
            + "if [[ ${#TPM_TARGETS[@]} -eq 0 ]]; then\n"
            + "  echo \"No LUKS containers selected for TPM integration; skipping.\"\n"
            + "else\n"
            + "  for container_dev in \"${TPM_TARGETS[@]}\"; do\n"
            + "    if ! cryptsetup isLuks \"$container_dev\" >/dev/null 2>&1; then\n"
            + "      echo \"$container_dev is not a LUKS container; skipping\" >&2\n"
            + "      continue\n"
            + "    fi\n"
            + "    prompt_password LUKS_CRYPT_PASSWORD \"Current LUKS password for $container_dev\"\n"
            + "    if [[ -z \"${LUKS_CRYPT_PASSWORD:-}\" ]]; then\n"
            + "      echo \"Current LUKS password is required\" >&2\n"
            + "      exit 1\n"
            + "    fi\n"
            + "    echo \"Binding clevis TPM2 to: $container_dev\"\n"
            + "    printf '%s' \"$LUKS_CRYPT_PASSWORD\" | clevis luks bind -y -k - -d \"$container_dev\" tpm2 \"$TPM2_POLICY\"\n"
            + "    echo \"List binding\"\n"
            + "    clevis luks list -d \"$container_dev\"\n"
            + "    printf '%s' \"$LUKS_CRYPT_PASSWORD\" | cryptsetup luksRemoveKey \"$container_dev\" -\n"
            + "  done\n"
            + "  if run_chroot command -v dracut >/dev/null 2>&1; then\n"
            + "    run_chroot dracut -q -f --regenerate-all\n"
            + "  elif run_chroot command -v update-initramfs >/dev/null 2>&1; then\n"
            + "    run_chroot update-initramfs -u -k all\n"
            + "  else\n"
            + "    echo \"No initramfs regeneration tool found in target OS\" >&2\n"
            + "    exit 1\n"
            + "  fi\n"
            + "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_WAIT_FOR_ENTER:
        message = str(cfg.get("message") or "Press Enter to continue.").strip()
        return (
            f'echo "[afterburner] {label}: wait for enter"\n'
            f'printf "%s\\n" {_shell_quote(message)}\n'
            'read -r _ < /dev/console || true\n'
        )

    if item.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
        script_body = str(cfg.get("script_body") or "").strip()
        lines = [
            f'echo "[afterburner] {label}: custom script"',
        ]
        for input_row in item.script_inputs.all().order_by("order", "id"):
            key = input_row.key
            label_text = input_row.label
            default_value = input_row.default_value or ""
            if input_row.input_type == AfterburnerScriptInput.TYPE_PASSWORD:
                lines.append(f'prompt_password {key} {_shell_quote(label_text)}')
            elif input_row.input_type == AfterburnerScriptInput.TYPE_BOOL:
                default_bool = "yes" if str(default_value).strip().lower() in {"1", "true", "yes", "y"} else "no"
                lines.append(f'prompt_bool {key} {_shell_quote(label_text)} {_shell_quote(default_bool)}')
            elif input_row.input_type == AfterburnerScriptInput.TYPE_SELECT:
                options = [str(v) for v in (input_row.select_options or []) if str(v).strip()]
                lines.append(f'prompt_text {key} {_shell_quote(label_text)} {_shell_quote(default_value)}')
                if options:
                    lines.append(f'if [[ -n "${{{key}:-}}" ]]; then')
                    lines.append(f'  case "${{{key}}}" in')
                    for opt in options:
                        lines.append(f"    {_shell_quote(opt)}) ;;")
                    lines.append(f'    *) echo "{key} must be one of: {", ".join(options)}" >&2; exit 1 ;;')
                    lines.append("  esac")
                    lines.append("fi")
            elif input_row.input_type == AfterburnerScriptInput.TYPE_INT:
                lines.append(f'prompt_text {key} {_shell_quote(label_text)} {_shell_quote(default_value)}')
                lines.append(f'if [[ -n "${{{key}:-}}" ]] && ! [[ "${{{key}}}" =~ ^[0-9]+$ ]]; then echo "{key} must be an integer" >&2; exit 1; fi')
            else:
                lines.append(f'prompt_text {key} {_shell_quote(label_text)} {_shell_quote(default_value)}')

            if input_row.required:
                lines.append(f'if [[ -z "${{{key}:-}}" ]]; then echo "{key} is required" >&2; exit 1; fi')

        if script_body:
            lines.append("cat <<'TUXWS_AFTERBURNER_SCRIPT' > /tmp/tuxws-afterburner-custom.sh")
            lines.append(script_body)
            lines.append("TUXWS_AFTERBURNER_SCRIPT")
            lines.append("chmod 700 /tmp/tuxws-afterburner-custom.sh")
            env_export = " ".join([f'{row.key}=\"${{{row.key}:-}}\"' for row in item.script_inputs.all().order_by("order", "id")])
            if env_export:
                lines.append(f"env {env_export} bash /tmp/tuxws-afterburner-custom.sh")
            else:
                lines.append("bash /tmp/tuxws-afterburner-custom.sh")
        else:
            lines.append("echo 'No custom script body configured; skipping.'")

        return "\n".join(lines) + "\n"

    return f'echo "[afterburner] Unknown item type for {label}; skipping"\n'


def render_afterburner_script(*, build, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "afterburner.sh"

    selections = list(build.ordered_afterburner_selections().select_related("afterburner").prefetch_related("afterburner__items", "afterburner__items__script_inputs"))

    body_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "TARGET_ROOT=\"${MOUNT_ROOT:-/mnt/sysimage}\"",
        "if [[ ! -d \"$TARGET_ROOT\" ]]; then",
        "  echo \"Target root $TARGET_ROOT is missing\" >&2",
        "  exit 1",
        "fi",
        "",
        "run_chroot() {",
        "  chroot \"$TARGET_ROOT\" /usr/bin/env \"$@\"",
        "}",
        "",
        "prompt_text() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local default_value=\"${3:-}\"",
        "  local value=\"\"",
        "  if [[ -n \"$default_value\" ]]; then",
        "    read -r -p \"$label [$default_value]: \" value || true",
        "    value=\"${value:-$default_value}\"",
        "  else",
        "    read -r -p \"$label: \" value || true",
        "  fi",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "prompt_password() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local value=\"\"",
        "  read -r -s -p \"$label: \" value || true",
        "  echo",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "prompt_bool() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local default_value=\"${3:-no}\"",
        "  local value=\"\"",
        "  read -r -p \"$label [${default_value}]: \" value || true",
        "  value=\"${value:-$default_value}\"",
        "  case \"${value,,}\" in",
        "    y|yes|true|1) value=\"yes\" ;;",
        "    *) value=\"no\" ;;",
        "  esac",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "if [[ ! -t 0 ]]; then",
        "  exec </dev/console >/dev/console 2>&1 || true",
        "fi",
        "",
        "echo",
        "echo \"=== TuxWSMaker Restore Afterburner ===\"",
        "echo \"Answer prompts to finalize machine setup.\"",
        "echo",
    ]

    if not selections:
        body_lines.append('echo "No afterburners attached to this build; nothing to run."')
    else:
        for sel in selections:
            profile = sel.afterburner
            body_lines.append(f'echo "--- Profile: {profile.name} ---"')
            for item in profile.items.all().order_by("order", "id"):
                body_lines.append(_build_item_snippet(item))

    body_lines.extend([
        "",
        "echo",
        "echo \"Afterburner completed successfully.\"",
    ])

    path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path
