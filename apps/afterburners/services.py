from __future__ import annotations

import base64
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
        hostname_value_answer_key = str(cfg.get("hostname_value_answer_key") or "")
        hostname_domain_answer_key = str(cfg.get("hostname_domain_answer_key") or "")
        return (
            f'echo "[afterburner] {label}: hostname"\n'
            "prompt_text_with_answer HOSTNAME_VALUE \"Hostname (short name)\" \"\" "
            + _shell_quote(hostname_value_answer_key)
            + "\n"
            "if [[ -n \"${HOSTNAME_VALUE:-}\" ]]; then\n"
            "  prompt_text_with_answer HOSTNAME_DOMAIN \"Domain name (optional)\" \"\" "
            + _shell_quote(hostname_domain_answer_key)
            + "\n"
            "  FQDN_VALUE=\"$HOSTNAME_VALUE\"\n"
            "  if [[ -n \"${HOSTNAME_DOMAIN:-}\" ]]; then\n"
            "    FQDN_VALUE=\"$HOSTNAME_VALUE.$HOSTNAME_DOMAIN\"\n"
            "  fi\n"
            "  run_chroot hostnamectl set-hostname \"$FQDN_VALUE\"\n"
            "  echo \"$FQDN_VALUE\" > \"$TARGET_ROOT/etc/hostname\"\n"
            "fi\n"
        )

    if item.item_type == AfterburnerItem.TYPE_LOCAL_USER:
        default_groups = str(cfg.get("groups") or "")
        prompt_groups = bool(cfg.get("prompt_groups") or False)
        username_answer_key = str(cfg.get("local_user_username_answer_key") or "")
        firstname_answer_key = str(cfg.get("local_user_firstname_answer_key") or "")
        lastname_answer_key = str(cfg.get("local_user_lastname_answer_key") or "")
        password_answer_key = str(cfg.get("local_user_password_answer_key") or "")
        admin_answer_key = str(cfg.get("local_user_admin_answer_key") or "")
        groups_answer_key = str(cfg.get("local_user_groups_answer_key") or "")
        group_prompt_block = (
            '  prompt_text_with_answer LOCAL_USER_GROUPS "Additional groups (comma-separated; blank to skip)" '
            + _shell_quote(default_groups)
            + " "
            + _shell_quote(groups_answer_key)
            + "\n"
            if prompt_groups
            else "  LOCAL_USER_GROUPS=" + _shell_quote(default_groups) + "\n"
        )
        return (
            f'echo "[afterburner] {label}: local user"\n'
            "while true; do\n"
            "  prompt_text_with_answer LOCAL_USER_USERNAME \"Username (blank to skip)\" \"\" "
            + _shell_quote(username_answer_key)
            + "\n"
            "  if [[ -z \"${LOCAL_USER_USERNAME:-}\" ]]; then\n"
            "    break\n"
            "  fi\n"
            "  prompt_text_with_answer LOCAL_USER_FIRSTNAME \"First name\" \"\" "
            + _shell_quote(firstname_answer_key)
            + "\n"
            "  prompt_text_with_answer LOCAL_USER_LASTNAME \"Last name\" \"\" "
            + _shell_quote(lastname_answer_key)
            + "\n"
            "  while true; do\n"
            "    prompt_password_with_answer LOCAL_USER_PASS_1 \"Password\" "
            + _shell_quote(password_answer_key)
            + "\n"
            "    prompt_password_with_answer LOCAL_USER_PASS_2 \"Confirm password\" "
            + _shell_quote(password_answer_key)
            + "\n"
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
            "  prompt_bool_with_answer LOCAL_USER_ADMIN \"Grant sudo (wheel) access\" \"no\" "
            + _shell_quote(admin_answer_key)
            + "\n"
            "  if run_chroot id \"$LOCAL_USER_USERNAME\" >/dev/null 2>&1; then\n"
            "    echo \"User $LOCAL_USER_USERNAME already exists; updating.\"\n"
            "    _useradd_ok=1\n"
            "  else\n"
            "    run_chroot useradd -m \"$LOCAL_USER_USERNAME\" 2>/tmp/_useradd_err.txt; _useradd_ok=$?\n"
            "    _useradd_err=$(cat /tmp/_useradd_err.txt 2>/dev/null || true)\n"
            "    if [[ $_useradd_ok -ne 0 ]]; then\n"
            "      echo \"useradd failed: $_useradd_err\" >&2\n"
            "      echo \"Please enter a valid username and try again.\" >&2\n"
            "      continue\n"
            "    fi\n"
            "  fi\n"
            + group_prompt_block
            + "  if [[ -n \"${LOCAL_USER_GROUPS//[[:space:],]/}\" ]]; then\n"
            + "    declare -A LOCAL_USER_GROUP_SEEN=()\n"
            + "    while IFS= read -r LOCAL_USER_GROUP; do\n"
            + "      LOCAL_USER_GROUP=\"$(echo \"$LOCAL_USER_GROUP\" | xargs)\"\n"
            + "      [[ -n \"$LOCAL_USER_GROUP\" ]] || continue\n"
            + "      [[ -z \"${LOCAL_USER_GROUP_SEEN[$LOCAL_USER_GROUP]:-}\" ]] || continue\n"
            + "      LOCAL_USER_GROUP_SEEN[$LOCAL_USER_GROUP]=1\n"
            + "      run_chroot groupadd -f \"$LOCAL_USER_GROUP\" >/dev/null 2>&1 || true\n"
            + "      run_chroot usermod -aG \"$LOCAL_USER_GROUP\" \"$LOCAL_USER_USERNAME\"\n"
            + "    done < <(printf '%s\\n' \"$LOCAL_USER_GROUPS\" | tr ',' '\\n')\n"
            + "  fi\n"
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
            "  break\n"
            "done\n"
        )

    if item.item_type == AfterburnerItem.TYPE_AD_JOIN:
        default_domain = str(cfg.get("domain") or "")
        default_ou = str(cfg.get("computer_ou") or "")
        default_join_user = str(cfg.get("join_user") or "")
        domain_answer_key = str(cfg.get("ad_domain_answer_key") or "")
        computer_ou_answer_key = str(cfg.get("ad_computer_ou_answer_key") or "")
        join_user_answer_key = str(cfg.get("ad_join_user_answer_key") or "")
        join_password_answer_key = str(cfg.get("ad_join_password_answer_key") or "")
        return (
            f'echo "[afterburner] {label}: AD join"\n'
            "prompt_text_with_answer AD_DOMAIN \"Active Directory domain (blank to skip)\" "
            + _shell_quote(default_domain)
            + " "
            + _shell_quote(domain_answer_key)
            + "\n"
            "if [[ -n \"${AD_DOMAIN:-}\" ]]; then\n"
            "  prompt_text_with_answer AD_JOIN_USER \"AD join username\" "
            + _shell_quote(default_join_user)
            + " "
            + _shell_quote(join_user_answer_key)
            + "\n"
            "  prompt_password_with_answer AD_JOIN_PASS \"AD join password\" "
            + _shell_quote(join_password_answer_key)
            + "\n"
            "  prompt_text_with_answer AD_COMPUTER_OU \"Computer OU (optional)\" "
            + _shell_quote(default_ou)
            + " "
            + _shell_quote(computer_ou_answer_key)
            + "\n"
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
        iface_answer_key = str(cfg.get("static_interface_answer_key") or "")
        ip_answer_key = str(cfg.get("static_ip_address_answer_key") or "")
        prefix_answer_key = str(cfg.get("static_prefix_answer_key") or "")
        gateway_answer_key = str(cfg.get("static_gateway_answer_key") or "")
        dns_answer_key = str(cfg.get("static_dns_answer_key") or "")
        return (
            f'echo "[afterburner] {label}: static ip"\n'
            "prompt_text_with_answer STATIC_IFACE \"Interface (blank to skip)\" "
            + _shell_quote(default_iface)
            + " "
            + _shell_quote(iface_answer_key)
            + "\n"
            "if [[ -n \"${STATIC_IFACE:-}\" ]]; then\n"
            "  prompt_text_with_answer STATIC_IP \"IPv4 address\" "
            + _shell_quote(default_ip)
            + " "
            + _shell_quote(ip_answer_key)
            + "\n"
            "  prompt_text_with_answer STATIC_PREFIX \"Prefix\" "
            + _shell_quote(default_prefix)
            + " "
            + _shell_quote(prefix_answer_key)
            + "\n"
            "  prompt_text_with_answer STATIC_GW \"Gateway\" "
            + _shell_quote(default_gw)
            + " "
            + _shell_quote(gateway_answer_key)
            + "\n"
            "  prompt_text_with_answer STATIC_DNS \"DNS (comma separated)\" "
            + _shell_quote(default_dns)
            + " "
            + _shell_quote(dns_answer_key)
            + "\n"
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
        device_answer_key = str(cfg.get("luks_device_answer_key") or "")
        current_password_answer_key = str(cfg.get("luks_current_password_answer_key") or "")
        new_password_answer_key = str(cfg.get("luks_new_password_answer_key") or "")
        return (
            f'echo "[afterburner] {label}: rotate LUKS password"\n'
            "rotate_luks_container() {\n"
            "  local luks_dev=\"$1\"\n"
            "  echo \"[afterburner] Rotating LUKS password for $luks_dev\"\n"
            "  # Try the known build-time passphrase first; fall back to prompting if it doesn't match.\n"
            "  local luks_current=\"${DEFAULT_LUKS_PASSWORD:-tuxwsmaker}\"\n"
            "  if ! printf '%s' \"$luks_current\" | cryptsetup open --test-passphrase \"$luks_dev\" 2>/dev/null; then\n"
            "    echo \"[afterburner] Build-time passphrase did not match $luks_dev — please enter the current LUKS password.\"\n"
            "    while true; do\n"
            "      prompt_password_with_answer luks_current \"Current LUKS password for $luks_dev\" "
            + _shell_quote(current_password_answer_key)
            + "\n"
            "      if [[ -z \"${luks_current:-}\" ]]; then\n"
            "        echo \"Password cannot be empty\" >&2; continue\n"
            "      fi\n"
            "      if printf '%s' \"$luks_current\" | cryptsetup open --test-passphrase \"$luks_dev\" 2>/dev/null; then\n"
            "        break\n"
            "      fi\n"
            "      echo \"Incorrect LUKS password — try again\" >&2\n"
            "    done\n"
            "  fi\n"
            "  while true; do\n"
            "    prompt_password_with_answer LUKS_NEW \"New LUKS password for $luks_dev\" "
            + _shell_quote(new_password_answer_key)
            + "\n"
            "    prompt_password_with_answer LUKS_NEW_CONFIRM \"Confirm new LUKS password for $luks_dev\" "
            + _shell_quote(new_password_answer_key)
            + "\n"
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
            "  # Write current passphrase to a temp key file so both add and remove\n"
            "  # operations read from the same source without stdin ambiguity.\n"
            "  local _luks_tmpkey\n"
            "  _luks_tmpkey=$(mktemp /tmp/luks-key-XXXXXX)\n"
            "  chmod 600 \"$_luks_tmpkey\"\n"
            "  printf '%s' \"$luks_current\" > \"$_luks_tmpkey\"\n"
            "  printf '%s' \"$LUKS_NEW\" | cryptsetup luksAddKey --key-file \"$_luks_tmpkey\" \"$luks_dev\" - || {\n"
            "    echo \"[afterburner] ERROR: Failed to add new LUKS key on $luks_dev\" >&2\n"
            "    rm -f \"$_luks_tmpkey\"; return 1\n"
            "  }\n"
            "  cryptsetup luksRemoveKey --key-file \"$_luks_tmpkey\" \"$luks_dev\" || {\n"
            "    echo \"[afterburner] ERROR: Failed to remove old LUKS key on $luks_dev\" >&2\n"
            "    rm -f \"$_luks_tmpkey\"; return 1\n"
            "  }\n"
            "  rm -f \"$_luks_tmpkey\"\n"
            "  echo \"[afterburner] LUKS password rotated successfully for $luks_dev\"\n"
            "}\n"
            "discover_luks_devices() {\n"
            "  resolve_crypttab_source() {\n"
            "    local source_spec=\"$1\"\n"
            "    case \"$source_spec\" in\n"
            "      UUID=*) blkid -U \"${source_spec#UUID=}\" 2>/dev/null || true ;;\n"
            "      LABEL=*) blkid -L \"${source_spec#LABEL=}\" 2>/dev/null || true ;;\n"
            "      /dev/*) printf '%s\\n' \"$source_spec\" ;;\n"
            "      *) return 0 ;;\n"
            "    esac\n"
            "  }\n"
            "  local discovered_from_crypttab=0\n"
            "  if [[ -f \"$TARGET_ROOT/etc/crypttab\" ]]; then\n"
            "    while IFS= read -r crypt_source; do\n"
            "      [[ -n \"${crypt_source:-}\" ]] || continue\n"
            "      while IFS= read -r candidate; do\n"
            "        [[ -n \"${candidate:-}\" ]] || continue\n"
            "        if cryptsetup isLuks \"$candidate\" >/dev/null 2>&1; then\n"
            "          printf '%s\\n' \"$candidate\"\n"
            "          discovered_from_crypttab=1\n"
            "        fi\n"
            "      done < <(resolve_crypttab_source \"$crypt_source\")\n"
            "    done < <(awk 'NF >= 2 && $1 !~ /^[[:space:]]*#/ {print $2}' \"$TARGET_ROOT/etc/crypttab\")\n"
            "  fi\n"
            "  if [[ $discovered_from_crypttab -eq 0 ]]; then\n"
            "    local -a fallback_candidates=()\n"
            "    while IFS= read -r candidate; do\n"
            "      [[ -n \"$candidate\" ]] || continue\n"
            "      if cryptsetup isLuks \"$candidate\" >/dev/null 2>&1; then\n"
            "        fallback_candidates+=(\"$candidate\")\n"
            "      fi\n"
            "    done < <(lsblk -rpn -o NAME,TYPE | awk '$2 == \"disk\" || $2 == \"part\" {print $1}')\n"
            "    if [[ ${#fallback_candidates[@]} -gt 1 ]]; then\n"
            "      echo \"[afterburner] ERROR: LUKS autodetect is ambiguous without $TARGET_ROOT/etc/crypttab guidance: ${fallback_candidates[*]}\" >&2\n"
            "      echo \"[afterburner] Set an explicit LUKS device in this afterburner item or fix crypttab in the restored target.\" >&2\n"
            "      return 1\n"
            "    fi\n"
            "    for candidate in \"${fallback_candidates[@]}\"; do\n"
            "      printf '%s\\n' \"$candidate\"\n"
            "    done\n"
            "  fi\n"
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
            + "LUKS_DEVICE_VALUE="
            + _shell_quote(default_device)
            + "\n"
            + "if [[ -n \"$LUKS_DEVICE_VALUE\" ]]; then\n"
            + "  prompt_text_with_answer LUKS_DEVICE_VALUE \"LUKS device path (optional when autodetect is enabled)\" \"$LUKS_DEVICE_VALUE\" "
            + _shell_quote(device_answer_key)
            + "\n"
            + "fi\n"
            + "add_luks_target \"$LUKS_DEVICE_VALUE\"\n"
            + (
                "LUKS_AUTODETECT_OUTPUT=\"$(discover_luks_devices)\"; LUKS_AUTODETECT_RC=$?\n"
                "if [[ $LUKS_AUTODETECT_RC -ne 0 ]]; then\n"
                "  exit $LUKS_AUTODETECT_RC\n"
                "fi\n"
                "while IFS= read -r luks_dev; do\n"
                "  [[ -n \"${luks_dev:-}\" ]] || continue\n"
                "  add_luks_target \"$luks_dev\"\n"
                "done <<< \"$LUKS_AUTODETECT_OUTPUT\"\n"
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
        user_answer_key = str(cfg.get("bootloader_user_answer_key") or "")
        password_answer_key = str(cfg.get("bootloader_password_answer_key") or "")
        return (
            f'echo "[afterburner] {label}: set bootloader password"\n'
            "prompt_text_with_answer GRUB_USER \"GRUB superuser\" "
            + _shell_quote(default_grub_user)
            + " "
            + _shell_quote(user_answer_key)
            + "\n"
            'if [[ -n "${GRUB_USER:-}" ]]; then\n'
            '  while true; do\n'
            '    prompt_password_with_answer GRUB_PW_1 "GRUB password" '
            + _shell_quote(password_answer_key)
            + '\n'
            '    prompt_password_with_answer GRUB_PW_2 "Confirm GRUB password" '
            + _shell_quote(password_answer_key)
            + '\n'
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
        use_activation_key_answer_key = str(cfg.get("rhsm_use_activation_key_answer_key") or "")
        username_answer_key = str(cfg.get("rhsm_username_answer_key") or "")
        password_answer_key = str(cfg.get("rhsm_password_answer_key") or "")
        org_id_answer_key = str(cfg.get("rhsm_org_id_answer_key") or "")
        activation_key_answer_key = str(cfg.get("rhsm_activation_key_answer_key") or "")
        repo_ids_answer_key = str(cfg.get("rhsm_repo_ids_answer_key") or "")
        if prompt_credentials:
            credentials_block = (
                "while true; do\n"
                "  RHSM_MODE_FROM_ANSWERS=\"\"\n"
                "  RHSM_ANSWERS_ORG_ID=\"\"\n"
                "  RHSM_ANSWERS_ACTIVATION_KEY=\"\"\n"
                "  RHSM_ANSWERS_USERNAME=\"\"\n"
                "  RHSM_ANSWERS_PASSWORD=\"\"\n"
                "  if lookup_answer "
                + _shell_quote(org_id_answer_key)
                + " RHSM_ANSWERS_ORG_ID && lookup_answer "
                + _shell_quote(activation_key_answer_key)
                + " RHSM_ANSWERS_ACTIVATION_KEY && [[ -n \"${RHSM_ANSWERS_ORG_ID//[[:space:]]/}\" && -n \"${RHSM_ANSWERS_ACTIVATION_KEY//[[:space:]]/}\" ]]; then\n"
                "    RHSM_MODE_FROM_ANSWERS=\"activation\"\n"
                "  elif lookup_answer "
                + _shell_quote(username_answer_key)
                + " RHSM_ANSWERS_USERNAME && lookup_answer "
                + _shell_quote(password_answer_key)
                + " RHSM_ANSWERS_PASSWORD && [[ -n \"${RHSM_ANSWERS_USERNAME//[[:space:]]/}\" && -n \"${RHSM_ANSWERS_PASSWORD//[[:space:]]/}\" ]]; then\n"
                "    RHSM_MODE_FROM_ANSWERS=\"userpass\"\n"
                "  fi\n"
                "  if [[ \"$RHSM_MODE_FROM_ANSWERS\" == \"activation\" ]]; then\n"
                "    if run_chroot subscription-manager register --force --org \"$RHSM_ANSWERS_ORG_ID\" --activationkey \"$RHSM_ANSWERS_ACTIVATION_KEY\"; then\n"
                "      break\n"
                "    fi\n"
                "    echo \"Red Hat registration failed using org/activation key from answers file.\" >&2\n"
                "    exit 1\n"
                "  elif [[ \"$RHSM_MODE_FROM_ANSWERS\" == \"userpass\" ]]; then\n"
                "    if run_chroot subscription-manager register --force --username \"$RHSM_ANSWERS_USERNAME\" --password \"$RHSM_ANSWERS_PASSWORD\"; then\n"
                "      break\n"
                "    fi\n"
                "    echo \"Red Hat registration failed using username/password from answers file.\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "  prompt_bool_with_answer RHSM_USE_ACTIVATION_KEY \"Use activation key registration\" \"no\" "
                + _shell_quote(use_activation_key_answer_key)
                + "\n"
                "  if [[ \"$RHSM_USE_ACTIVATION_KEY\" == \"yes\" ]]; then\n"
                "    prompt_text_with_answer RHSM_ORG_ID \"Organization ID\" \"\" "
                + _shell_quote(org_id_answer_key)
                + "\n"
                "    prompt_text_with_answer RHSM_ACTIVATION_KEY \"Activation key\" \"\" "
                + _shell_quote(activation_key_answer_key)
                + "\n"
                "    if [[ -z \"${RHSM_ORG_ID//[[:space:]]/}\" ]]; then\n"
                "      prompt_text RHSM_ORG_ID \"Organization ID\" \"\"\n"
                "    fi\n"
                "    if [[ -z \"${RHSM_ACTIVATION_KEY//[[:space:]]/}\" ]]; then\n"
                "      prompt_text RHSM_ACTIVATION_KEY \"Activation key\" \"\"\n"
                "    fi\n"
                "    if [[ -z \"${RHSM_ORG_ID//[[:space:]]/}\" || -z \"${RHSM_ACTIVATION_KEY//[[:space:]]/}\" ]]; then\n"
                "      echo \"Org ID and activation key are required\" >&2\n"
                "      continue\n"
                "    fi\n"
                "    if run_chroot subscription-manager register --force --org \"$RHSM_ORG_ID\" --activationkey \"$RHSM_ACTIVATION_KEY\"; then\n"
                "      break\n"
                "    fi\n"
                "  else\n"
                "    prompt_text_with_answer RHSM_USERNAME \"Red Hat username\" \"\" "
                + _shell_quote(username_answer_key)
                + "\n"
                "    prompt_password_with_answer RHSM_PASSWORD \"Red Hat password\" "
                + _shell_quote(password_answer_key)
                + "\n"
                "    if [[ -z \"${RHSM_USERNAME//[[:space:]]/}\" ]]; then\n"
                "      prompt_text RHSM_USERNAME \"Red Hat username\" \"\"\n"
                "    fi\n"
                "    if [[ -z \"${RHSM_PASSWORD//[[:space:]]/}\" ]]; then\n"
                "      prompt_password RHSM_PASSWORD \"Red Hat password\"\n"
                "    fi\n"
                "    if [[ -z \"${RHSM_USERNAME//[[:space:]]/}\" || -z \"${RHSM_PASSWORD//[[:space:]]/}\" ]]; then\n"
                "      echo \"Username and password are required\" >&2\n"
                "      continue\n"
                "    fi\n"
                "    if run_chroot subscription-manager register --force --username \"$RHSM_USERNAME\" --password \"$RHSM_PASSWORD\"; then\n"
                "      break\n"
                "    fi\n"
                "  fi\n"
                "  echo \"Red Hat registration failed. Try again.\" >&2\n"
                "done\n"
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
            "prompt_text_with_answer RHSM_REPO_IDS_USER \"Repository IDs (comma-separated; blank to skip)\" "
            + _shell_quote(default_repo_ids)
            + " "
            + _shell_quote(repo_ids_answer_key)
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
        device_answer_key = str(cfg.get("tpm_device_answer_key") or "")
        password_answer_key = str(cfg.get("tpm_password_answer_key") or "")

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

        policy_candidates: list[dict[str, str]] = [tpm_policy_payload]
        relaxed_policy: dict[str, str] = {}
        if tpm_pcr_bank:
            relaxed_policy["pcr_bank"] = tpm_pcr_bank
        if pcr_ids:
            relaxed_policy["pcr_ids"] = ",".join(pcr_ids)
        if relaxed_policy:
            policy_candidates.append(relaxed_policy)
        policy_candidates.append({})

        serialized_candidates: list[str] = []
        for payload in policy_candidates:
            encoded = json.dumps(payload, separators=(",", ":"))
            if encoded not in serialized_candidates:
                serialized_candidates.append(encoded)

        tpm_policy_candidates_b64 = [
            base64.b64encode(policy.encode("utf-8")).decode("ascii")
            for policy in serialized_candidates
        ]
        tpm_policy_candidates_shell = " ".join(_shell_quote(value) for value in tpm_policy_candidates_b64)

        return (
            f'echo "[afterburner] {label}: tpm integration"\n'
            'if ! run_chroot command -v clevis >/dev/null 2>&1; then\n'
            '  echo "clevis is not available in target OS; skipping TPM integration" >&2\n'
            '  exit 1\n'
            'fi\n'
            'if ! run_chroot command -v clevis-pin-tpm2 >/dev/null 2>&1; then\n'
            '  echo "clevis tpm2 pin is unavailable in target OS; install clevis-pin-tpm2 in the restored image" >&2\n'
            '  exit 1\n'
            'fi\n'
            'if ! command -v cryptsetup >/dev/null 2>&1; then\n'
            '  echo "cryptsetup is not available; skipping TPM integration" >&2\n'
            '  exit 1\n'
            'fi\n'
            'if [[ ! -c /dev/tpmrm0 && ! -c /dev/tpm0 ]]; then\n'
            '  echo "No TPM device found (/dev/tpmrm0 or /dev/tpm0); skipping TPM integration"\n'
            '  exit 0\n'
            'fi\n'
            'TPM2_POLICY_B64_CANDIDATES=(' + tpm_policy_candidates_shell + ')\n'
            "discover_tpm_luks_devices() {\n"
            "  resolve_crypttab_source() {\n"
            "    local source_spec=\"$1\"\n"
            "    case \"$source_spec\" in\n"
            "      UUID=*) blkid -U \"${source_spec#UUID=}\" 2>/dev/null || true ;;\n"
            "      LABEL=*) blkid -L \"${source_spec#LABEL=}\" 2>/dev/null || true ;;\n"
            "      /dev/*) printf '%s\\n' \"$source_spec\" ;;\n"
            "      *) return 0 ;;\n"
            "    esac\n"
            "  }\n"
            "  local discovered_from_crypttab=0\n"
            "  if [[ -f \"$TARGET_ROOT/etc/crypttab\" ]]; then\n"
            "    while IFS= read -r crypt_source; do\n"
            "      [[ -n \"${crypt_source:-}\" ]] || continue\n"
            "      while IFS= read -r candidate; do\n"
            "        [[ -n \"${candidate:-}\" ]] || continue\n"
            "        if cryptsetup isLuks \"$candidate\" >/dev/null 2>&1; then\n"
            "          printf '%s\\n' \"$candidate\"\n"
            "          discovered_from_crypttab=1\n"
            "        fi\n"
            "      done < <(resolve_crypttab_source \"$crypt_source\")\n"
            "    done < <(awk 'NF >= 2 && $1 !~ /^[[:space:]]*#/ {print $2}' \"$TARGET_ROOT/etc/crypttab\")\n"
            "  fi\n"
            "  if [[ $discovered_from_crypttab -eq 0 ]]; then\n"
            "    local -a fallback_candidates=()\n"
            "    while IFS= read -r candidate; do\n"
            "      [[ -n \"$candidate\" ]] || continue\n"
            "      if cryptsetup isLuks \"$candidate\" >/dev/null 2>&1; then\n"
            "        fallback_candidates+=(\"$candidate\")\n"
            "      fi\n"
            "    done < <(lsblk -rpn -o NAME,TYPE | awk '$2 == \"disk\" || $2 == \"part\" {print $1}')\n"
            "    if [[ ${#fallback_candidates[@]} -gt 1 ]]; then\n"
            "      echo \"[afterburner] ERROR: TPM LUKS autodetect is ambiguous without $TARGET_ROOT/etc/crypttab guidance: ${fallback_candidates[*]}\" >&2\n"
            "      echo \"[afterburner] Set an explicit TPM integration device in this afterburner item or fix crypttab in the restored target.\" >&2\n"
            "      return 1\n"
            "    fi\n"
            "    for candidate in \"${fallback_candidates[@]}\"; do\n"
            "      printf '%s\\n' \"$candidate\"\n"
            "    done\n"
            "  fi\n"
            "}\n"
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
            + "TPM_DEVICE_VALUE="
            + _shell_quote(default_device)
            + "\n"
            + "if [[ -n \"$TPM_DEVICE_VALUE\" ]]; then\n"
            + "  prompt_text_with_answer TPM_DEVICE_VALUE \"LUKS device path (optional when autodetect is enabled)\" \"$TPM_DEVICE_VALUE\" "
            + _shell_quote(device_answer_key)
            + "\n"
            + "fi\n"
            + "add_tpm_target \"$TPM_DEVICE_VALUE\"\n"
            + (
                "TPM_AUTODETECT_OUTPUT=\"$(discover_tpm_luks_devices)\"; TPM_AUTODETECT_RC=$?\n"
                "if [[ $TPM_AUTODETECT_RC -ne 0 ]]; then\n"
                "  exit $TPM_AUTODETECT_RC\n"
                "fi\n"
                "while IFS= read -r luks_dev; do\n"
                "  [[ -n \"$luks_dev\" ]] || continue\n"
                "  add_tpm_target \"$luks_dev\"\n"
                "done <<< \"$TPM_AUTODETECT_OUTPUT\"\n"
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
            + "    luks_tpm_pass=''\n"
            + "    while true; do\n"
            + "      prompt_password_with_answer luks_tpm_pass \"Current LUKS password for $container_dev\" "
            + _shell_quote(password_answer_key)
            + "\n"
            + "      if [[ -z \"${luks_tpm_pass:-}\" ]]; then\n"
            + "        echo \"Password cannot be empty\" >&2; continue\n"
            + "      fi\n"
            + "      if printf '%s' \"$luks_tpm_pass\" | cryptsetup open --test-passphrase \"$container_dev\" 2>/dev/null; then\n"
            + "        break\n"
            + "      fi\n"
            + "      echo \"Incorrect LUKS password — try again\" >&2\n"
            + "    done\n"
            + "    echo \"Binding clevis TPM2 to: $container_dev\"\n"
            + "    mkdir -p \"$TARGET_ROOT/tmp\"\n"
            + "    _clevis_tmpkey=$(mktemp \"$TARGET_ROOT/tmp/clevis-key-XXXXXX\")\n"
            + "    _clevis_tmpkey_chroot=\"/tmp/${_clevis_tmpkey##*/}\"\n"
            + "    chmod 600 \"$_clevis_tmpkey\"\n"
            + "    printf '%s' \"$luks_tpm_pass\" > \"$_clevis_tmpkey\"\n"
            + "    tpm_bind_ok=0\n"
            + "    for TPM2_POLICY_B64 in \"${TPM2_POLICY_B64_CANDIDATES[@]}\"; do\n"
            + "      TPM2_POLICY=\"$(printf %s \"$TPM2_POLICY_B64\" | base64 -d)\"\n"
            + "      if run_chroot clevis luks bind -y -k \"$_clevis_tmpkey_chroot\" -d \"$container_dev\" tpm2 \"$TPM2_POLICY\"; then\n"
            + "        tpm_bind_ok=1\n"
            + "        break\n"
            + "      fi\n"
            + "      echo \"[afterburner] TPM2 bind attempt failed for policy: $TPM2_POLICY\" >&2\n"
            + "    done\n"
            + "    if [[ $tpm_bind_ok -ne 1 ]]; then\n"
            + "      echo \"[afterburner] ERROR: clevis TPM2 bind failed on $container_dev\" >&2\n"
            + "      rm -f \"$_clevis_tmpkey\"\n"
            + "      continue\n"
            + "    fi\n"
            + "    rm -f \"$_clevis_tmpkey\"\n"
            + "    echo \"Clevis bindings on $container_dev:\"\n"
            + "    run_chroot clevis luks list -d \"$container_dev\"\n"
            + "    printf '%s' \"$luks_tpm_pass\" | cryptsetup luksRemoveKey \"$container_dev\" -\n"
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
            'wait_for_enter "Press Enter to continue and reboot the restored system."\n'
        )

    if item.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
        script_body = str(cfg.get("script_body") or "")
        script_body = script_body.replace("\r\n", "\n").replace("\r", "\n").strip()
        run_mode = str(cfg.get("run_mode") or "non_chroot").strip().lower()
        if run_mode not in {"non_chroot", "chroot"}:
            run_mode = "non_chroot"
        lines = [
            f'echo "[afterburner] {label}: custom script ({run_mode})"',
        ]
        for input_row in item.script_inputs.all().order_by("order", "id"):
            key = input_row.key
            label_text = input_row.label
            default_value = input_row.default_value or ""
            answer_key = str(input_row.answer_key or "").strip()
            if input_row.input_type == AfterburnerScriptInput.TYPE_PASSWORD:
                lines.append(f'prompt_password_with_answer {key} {_shell_quote(label_text)} {_shell_quote(answer_key)}')
            elif input_row.input_type == AfterburnerScriptInput.TYPE_BOOL:
                default_bool = "yes" if str(default_value).strip().lower() in {"1", "true", "yes", "y"} else "no"
                lines.append(f'prompt_bool_with_answer {key} {_shell_quote(label_text)} {_shell_quote(default_bool)} {_shell_quote(answer_key)}')
            elif input_row.input_type == AfterburnerScriptInput.TYPE_SELECT:
                options = [str(v) for v in (input_row.select_options or []) if str(v).strip()]
                lines.append(f'prompt_text_with_answer {key} {_shell_quote(label_text)} {_shell_quote(default_value)} {_shell_quote(answer_key)}')
                if options:
                    lines.append(f'if [[ -n "${{{key}:-}}" ]]; then')
                    lines.append(f'  case "${{{key}}}" in')
                    for opt in options:
                        lines.append(f"    {_shell_quote(opt)}) ;;")
                    lines.append(f'    *) echo "{key} must be one of: {", ".join(options)}" >&2; exit 1 ;;')
                    lines.append("  esac")
                    lines.append("fi")
            elif input_row.input_type == AfterburnerScriptInput.TYPE_INT:
                lines.append(f'prompt_text_with_answer {key} {_shell_quote(label_text)} {_shell_quote(default_value)} {_shell_quote(answer_key)}')
                lines.append(f'if [[ -n "${{{key}:-}}" ]] && ! [[ "${{{key}}}" =~ ^[0-9]+$ ]]; then echo "{key} must be an integer" >&2; exit 1; fi')
            else:
                lines.append(f'prompt_text_with_answer {key} {_shell_quote(label_text)} {_shell_quote(default_value)} {_shell_quote(answer_key)}')

            if input_row.required:
                lines.append(f'if [[ -z "${{{key}:-}}" ]]; then echo "{key} is required" >&2; exit 1; fi')

        if script_body:
            if run_mode == "chroot":
                script_path = "$TARGET_ROOT/tmp/tuxws-afterburner-custom.sh"
                invoke_path = "/tmp/tuxws-afterburner-custom.sh"
            else:
                script_path = "/tmp/tuxws-afterburner-custom.sh"
                invoke_path = "/tmp/tuxws-afterburner-custom.sh"

            lines.append(f"cat <<'TUXWS_AFTERBURNER_SCRIPT' > {script_path}")
            lines.append(script_body)
            lines.append("TUXWS_AFTERBURNER_SCRIPT")
            lines.append(f"chmod 700 {script_path}")
            env_export = " ".join([f'{row.key}=\"${{{row.key}:-}}\"' for row in item.script_inputs.all().order_by("order", "id")])
            if run_mode == "chroot":
                if env_export:
                    lines.append(f"run_chroot {env_export} bash {invoke_path}")
                else:
                    lines.append(f"run_chroot bash {invoke_path}")
                lines.append(f"rm -f {script_path}")
            else:
                if env_export:
                    lines.append(f"env {env_export} bash {invoke_path}")
                else:
                    lines.append(f"bash {invoke_path}")
                lines.append(f"rm -f {script_path}")
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
        "  value=\"${value//$'\\r'/}\"",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "prompt_password() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local value=\"\"",
        "  read -r -s -p \"$label: \" value || true",
        "  echo",
        "  value=\"${value//$'\\r'/}\"",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "prompt_bool() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local default_value=\"${3:-no}\"",
        "  local value=\"\"",
        "  read -r -p \"$label [${default_value}]: \" value || true",
        "  value=\"${value//$'\\r'/}\"",
        "  value=\"${value:-$default_value}\"",
        "  case \"${value,,}\" in",
        "    y|yes|true|1) value=\"yes\" ;;",
        "    *) value=\"no\" ;;",
        "  esac",
        "  printf -v \"$var_name\" '%s' \"$value\"",
        "}",
        "",
        "declare -A ANSWERS_VALUES=()",
        "ANSWERS_LOADED=0",
        "ANSWERS_PATH=\"${ANSWERS_FILE:-}\"",
        "",
        "load_answers_file() {",
        "  [[ \"$ANSWERS_LOADED\" == \"1\" ]] && return 0",
        "  ANSWERS_LOADED=1",
        "  [[ -n \"$ANSWERS_PATH\" ]] || return 0",
        "  [[ -f \"$ANSWERS_PATH\" ]] || return 0",
        "",
        "  if command -v python3 >/dev/null 2>&1; then",
        "    while IFS=$'\\t' read -r answer_key answer_value; do",
        "      [[ -n \"${answer_key:-}\" ]] || continue",
        "      ANSWERS_VALUES[$answer_key]=\"$answer_value\"",
        "    done < <(python3 - \"$ANSWERS_PATH\" <<'PY'",
        "import sys",
        "from pathlib import Path",
        "",
        "path = Path(sys.argv[1])",
        "for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():",
        "    line = raw.strip()",
        "    if not line or line.startswith('#'):",
        "        continue",
        "    if ':' not in line:",
        "        continue",
        "    key, value = line.split(':', 1)",
        "    key = key.strip()",
        "    value = value.strip()",
        "    if not key:",
        "        continue",
        "    if len(value) >= 2 and ((value[0] == value[-1] == '\"') or (value[0] == value[-1] == \"'\")):",
        "        value = value[1:-1]",
        "    print(f\"{key}\\t{value}\")",
        "PY",
        "    )",
        "    return 0",
        "  fi",
        "",
        "  while IFS= read -r raw_line; do",
        "    line=\"${raw_line%%#*}\"",
        "    line=\"${line//$'\\r'/}\"",
        "    [[ \"$line\" == *:* ]] || continue",
        "    answer_key=\"${line%%:*}\"",
        "    answer_value=\"${line#*:}\"",
        "    answer_key=\"$(echo \"$answer_key\" | xargs)\"",
        "    answer_value=\"$(echo \"$answer_value\" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')\"",
        "    [[ -n \"$answer_key\" ]] || continue",
        "    ANSWERS_VALUES[$answer_key]=\"$answer_value\"",
        "  done < \"$ANSWERS_PATH\"",
        "}",
        "",
        "lookup_answer() {",
        "  local answer_key=\"$1\"",
        "  local out_var=\"$2\"",
        "  [[ -n \"$answer_key\" ]] || return 1",
        "  load_answers_file",
        "  if [[ -v ANSWERS_VALUES[$answer_key] ]]; then",
        "    printf -v \"$out_var\" '%s' \"${ANSWERS_VALUES[$answer_key]}\"",
        "    return 0",
        "  fi",
        "  return 1",
        "}",
        "",
        "prompt_text_with_answer() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local default_value=\"${3:-}\"",
        "  local answer_key=\"${4:-}\"",
        "  local value=\"\"",
        "  if lookup_answer \"$answer_key\" value; then",
        "    printf -v \"$var_name\" '%s' \"$value\"",
        "    return 0",
        "  fi",
        "  prompt_text \"$var_name\" \"$label\" \"$default_value\"",
        "}",
        "",
        "prompt_password_with_answer() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local answer_key=\"${3:-}\"",
        "  local value=\"\"",
        "  if lookup_answer \"$answer_key\" value; then",
        "    printf -v \"$var_name\" '%s' \"$value\"",
        "    return 0",
        "  fi",
        "  prompt_password \"$var_name\" \"$label\"",
        "}",
        "",
        "prompt_bool_with_answer() {",
        "  local var_name=\"$1\"",
        "  local label=\"$2\"",
        "  local default_value=\"${3:-no}\"",
        "  local answer_key=\"${4:-}\"",
        "  local value=\"\"",
        "  if lookup_answer \"$answer_key\" value; then",
        "    case \"${value,,}\" in",
        "      y|yes|true|1) value=\"yes\" ;;",
        "      *) value=\"no\" ;;",
        "    esac",
        "    printf -v \"$var_name\" '%s' \"$value\"",
        "    return 0",
        "  fi",
        "  prompt_bool \"$var_name\" \"$label\" \"$default_value\"",
        "}",
        "",
        "wait_for_enter() {",
        "  local prompt=\"${1:-Press Enter to continue.}\"",
        "  if [[ -n \"${DEPLOY_INPUT_TTY:-}\" && -c \"${DEPLOY_INPUT_TTY}\" ]]; then",
        "    read -r -p \"$prompt \" _ < \"${DEPLOY_INPUT_TTY}\" > \"${DEPLOY_INPUT_TTY}\" 2>&1 || true",
        "    return",
        "  fi",
        "  if [[ -t 0 ]]; then",
        "    read -r -p \"$prompt \" _ || true",
        "    return",
        "  fi",
        "  if [[ -c /dev/tty ]]; then",
        "    read -r -p \"$prompt \" _ < /dev/tty > /dev/tty 2>&1 || true",
        "    return",
        "  fi",
        "  if [[ -c /dev/console ]]; then",
        "    read -r -p \"$prompt \" _ < /dev/console > /dev/console 2>&1 || true",
        "    return",
        "  fi",
        "  echo \"$prompt\"",
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
