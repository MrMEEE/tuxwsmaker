from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urljoin
from urllib.parse import unquote
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


class RedHatDownloadError(RuntimeError):
    pass


@dataclass
class RedHatImageItem:
    label: str
    url: str
    major_version: str


@dataclass
class RedHatISOItem:
    label: str
    url: str
    version: str
    major_version: str


class RedHatDownloadClient:
    DOWNLOADS_URL = "https://access.redhat.com/downloads/content/479/"
    SSO_AUTH_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/auth"

    def __init__(
        self,
        username: str,
        password: str,
        timeout: int = 30,
        min_request_interval: float = 0.35,
        max_rate_limit_retries: int = 4,
    ) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "tuxwsmaker/1.0"})
        self.last_debug: dict[str, Any] = {}
        self._last_request_monotonic = 0.0

    def list_qcow2_images(self) -> list[RedHatImageItem]:
        response = self._get_authenticated_downloads_page()
        items = self._extract_qcow2_links(response.text)
        self.last_debug.update(
            {
                "list_url": response.url,
                "list_status": response.status_code,
                "list_html_bytes": len(response.text or ""),
                "list_items": len(items),
                "list_host": urlparse(response.url).netloc,
            }
        )
        return items

    def list_rhel_images(self) -> tuple[list[RedHatImageItem], list[RedHatISOItem]]:
        response = self._get_authenticated_downloads_page()
        qcow2_items = self._extract_qcow2_links(response.text)
        iso_items = self._extract_rhel_iso_items_from_downloads_page(response)
        self.last_debug.update(
            {
                "list_url": response.url,
                "list_status": response.status_code,
                "list_html_bytes": len(response.text or ""),
                "list_qcow2_items": len(qcow2_items),
                "list_iso_items": len(iso_items),
                "list_total_items": len(qcow2_items) + len(iso_items),
                "list_host": urlparse(response.url).netloc,
            }
        )
        return qcow2_items, iso_items

    def list_rhel_iso_images(self) -> list[RedHatISOItem]:
        pages = self.list_rhel_iso_version_pages()
        items_by_url: dict[str, RedHatISOItem] = {}
        scanned_pages = 0

        for page in pages:
            for item in self.list_rhel_iso_images_for_version_page(page["url"]):
                items_by_url[item.url] = item
            scanned_pages += 1

        result = sorted(
            items_by_url.values(),
            key=lambda item: (int(item.major_version), self._version_sort_key(item.version), item.label),
            reverse=True,
        )
        discovered_versions: dict[str, list[str]] = {}
        for page in pages:
            major = page.get("major", "")
            version = page.get("version", "")
            if not major or not version:
                continue
            discovered_versions[major] = sorted(
                set(discovered_versions.get(major, []) + [version]),
                key=self._version_sort_key,
                reverse=True,
            )
        self.last_debug["iso_versions_found"] = discovered_versions
        self.last_debug["iso_pages_seeded"] = len(pages)
        self.last_debug["iso_pages_queued_total"] = len(pages)
        self.last_debug["iso_pages_scanned"] = scanned_pages
        self.last_debug["iso_items"] = len(result)
        return result

    def _get_authenticated_downloads_page(self) -> requests.Response:
        self._login()
        response = self._request("get", self.DOWNLOADS_URL)
        return self._complete_sso_handoffs(response)

    def _discover_rhel_iso_version_pages(self, response: requests.Response) -> list[dict[str, str]]:
        product_pages = self._extract_product_software_pages(response.text)
        if not product_pages:
            product_pages = [response.url]

        seed_pages = list(dict.fromkeys(product_pages))
        pages_to_scan: dict[str, dict[str, str]] = {}

        for seed_url in seed_pages:
            seed_response = self._request("get", seed_url)
            seed_response = self._complete_sso_handoffs(seed_response)

            version_page_urls = self._extract_version_page_urls_from_dropdown(seed_response.text, seed_response.url)
            if not version_page_urls:
                version_page_urls = [seed_response.url]

            for version_page_url in version_page_urls:
                info = self._parse_product_software_url(version_page_url)
                if not info:
                    continue
                pages_to_scan[version_page_url] = info

        pages = [
            {
                "url": page_url,
                "major": info["major"],
                "version": info["version"],
            }
            for page_url, info in sorted(
                pages_to_scan.items(),
                key=lambda item: (int(item[1]["major"]), self._version_sort_key(item[1]["version"])),
                reverse=True,
            )
        ]

        discovered_versions: dict[str, list[str]] = {}
        for page in pages:
            major = page["major"]
            version = page["version"]
            discovered_versions[major] = sorted(
                set(discovered_versions.get(major, []) + [version]),
                key=self._version_sort_key,
                reverse=True,
            )

        self.last_debug["iso_versions_found"] = discovered_versions
        self.last_debug["iso_pages_seeded"] = len(seed_pages)
        self.last_debug["iso_pages_queued_total"] = len(pages)
        return pages

    def _extract_rhel_iso_items_from_downloads_page(self, response: requests.Response) -> list[RedHatISOItem]:
        pages = self._discover_rhel_iso_version_pages(response)
        items_by_url: dict[str, RedHatISOItem] = {}
        scanned_pages = 0

        for page in pages:
            for item in self.list_rhel_iso_images_for_version_page(page["url"]):
                items_by_url[item.url] = item
            scanned_pages += 1

        result = sorted(
            items_by_url.values(),
            key=lambda item: (int(item.major_version), self._version_sort_key(item.version), item.label),
            reverse=True,
        )
        self.last_debug["iso_pages_scanned"] = scanned_pages
        self.last_debug["iso_items"] = len(result)
        return result

    def list_rhel_iso_version_pages(self) -> list[dict[str, str]]:
        self._login()
        response = self._request("get", self.DOWNLOADS_URL)
        response = self._complete_sso_handoffs(response)

        product_pages = self._extract_product_software_pages(response.text)
        if not product_pages:
            product_pages = [response.url]

        seed_pages = list(dict.fromkeys(product_pages))
        pages_to_scan: dict[str, dict[str, str]] = {}

        for seed_url in seed_pages:
            seed_response = self._request("get", seed_url)
            seed_response = self._complete_sso_handoffs(seed_response)

            version_page_urls = self._extract_version_page_urls_from_dropdown(seed_response.text, seed_response.url)
            if not version_page_urls:
                version_page_urls = [seed_response.url]

            for version_page_url in version_page_urls:
                info = self._parse_product_software_url(version_page_url)
                if not info:
                    continue
                pages_to_scan[version_page_url] = info

        pages = [
            {
                "url": page_url,
                "major": info["major"],
                "version": info["version"],
            }
            for page_url, info in sorted(
                pages_to_scan.items(),
                key=lambda item: (int(item[1]["major"]), self._version_sort_key(item[1]["version"])),
                reverse=True,
            )
        ]

        discovered_versions: dict[str, list[str]] = {}
        for page in pages:
            major = page["major"]
            version = page["version"]
            discovered_versions[major] = sorted(
                set(discovered_versions.get(major, []) + [version]),
                key=self._version_sort_key,
                reverse=True,
            )

        self.last_debug["iso_versions_found"] = discovered_versions
        self.last_debug["iso_pages_seeded"] = len(seed_pages)
        self.last_debug["iso_pages_queued_total"] = len(pages)
        return pages

    def list_rhel_iso_images_for_version_page(self, page_url: str) -> list[RedHatISOItem]:
        page_response = self._request("get", page_url)
        page_response = self._complete_sso_handoffs(page_response)
        info = self._parse_product_software_url(page_response.url) or self._parse_product_software_url(page_url)
        if not info:
            return []
        items = self._extract_iso_links_from_product_page(
            page_response.text,
            page_response.url,
            info["major"],
            info["version"],
            dvd_only=True,
        )
        self.last_debug["iso_last_page_url"] = page_response.url
        self.last_debug["iso_last_page_items"] = len(items)
        return items

    def download_image(self, *, image_url: str, output_dir: Path) -> Path:
        self._login()
        output_dir.mkdir(parents=True, exist_ok=True)

        self._throttle_before_request()
        with self.session.get(image_url, timeout=self.timeout, stream=True, allow_redirects=True) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "html" in content_type:
                raise RedHatDownloadError(f"Expected a binary artifact but received HTML from {resp.url}")
            filename = self._resolve_filename(resp, image_url)
            target = output_dir / filename
            with target.open("wb") as file_obj:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_obj.write(chunk)

        return target

    def _login(self) -> None:
        downloads_page = self._request("get", self.DOWNLOADS_URL)
        self.last_debug = {
            "login_initial_url": downloads_page.url,
            "login_initial_status": downloads_page.status_code,
            "login_initial_bytes": len(downloads_page.text or ""),
        }

        login_page = downloads_page
        if "sso.redhat.com" not in urlparse(downloads_page.url).netloc.lower():
            soup = BeautifulSoup(downloads_page.text, "html.parser")
            login_link = None
            for anchor in soup.find_all("a", href=True):
                href = self._attr_to_str(anchor.get("href"))
                if "sso.redhat.com" in href and "openid-connect/auth" in href:
                    login_link = href
                    break
                if "/login" in href and "redirectTo=" in href:
                    login_link = urljoin(downloads_page.url, href)
                    break

            if not login_link:
                self.last_debug["login_link_found"] = False
                return
            self.last_debug["login_link_found"] = True
            self.last_debug["login_link"] = login_link

            login_page = self._request("get", login_link, allow_redirects=True)
        else:
            self.last_debug["login_link_found"] = True
            self.last_debug["login_link"] = downloads_page.url

        self.last_debug["login_page_url"] = login_page.url
        self.last_debug["login_page_status"] = login_page.status_code

        login_soup = BeautifulSoup(login_page.text, "html.parser")
        form = self._select_login_form(login_soup)
        self.last_debug["login_forms_found"] = len(login_soup.find_all("form"))
        if form is None:
            self.last_debug["login_form_missing"] = True
            login_page = self._fetch_keycloak_login_page()
            login_soup = BeautifulSoup(login_page.text, "html.parser")
            form = self._select_login_form(login_soup)
            self.last_debug["login_forms_found"] = len(login_soup.find_all("form"))
            if form is None:
                raise RedHatDownloadError("Could not find Red Hat login form")
            self.last_debug["login_mode"] = "keycloak-direct"
        else:
            self.last_debug["login_mode"] = "portal-form"
        self.last_debug["login_form_id"] = form.get("id")

        action = self._attr_to_str(form.get("action"))
        if not action:
            action = self._extract_action_from_html(login_page.text)
        if not action:
            raise RedHatDownloadError("Could not find Red Hat login endpoint")

        action_url = urljoin(login_page.url, action)
        payload: dict[str, Any] = {}
        for input_tag in form.find_all("input"):
            name = self._attr_to_str(input_tag.get("name"))
            if not name:
                continue
            payload[name] = self._attr_to_str(input_tag.get("value"))

        if "username" in payload:
            payload["username"] = self.username
        else:
            payload["email"] = self.username

        if "password" in payload:
            payload["password"] = self.password

        response = self._request("post", action_url, data=payload, allow_redirects=True)
        self.last_debug["login_post_url"] = response.url
        self.last_debug["login_post_status"] = response.status_code

        post_login_page = self._request("get", self.DOWNLOADS_URL)
        post_login_page = self._complete_sso_handoffs(post_login_page)
        self.last_debug["post_login_url"] = post_login_page.url
        self.last_debug["post_login_status"] = post_login_page.status_code
        self.last_debug["post_login_bytes"] = len(post_login_page.text or "")
        if "Log in for full access" in post_login_page.text and "SUBSCRIBER EXCLUSIVE CONTENT" in post_login_page.text:
            raise RedHatDownloadError("Red Hat login failed or account lacks download entitlement")

    def _complete_sso_handoffs(self, response: requests.Response) -> requests.Response:
        hops: list[dict[str, Any]] = []
        max_hops = 6
        downloads_path = urlparse(self.DOWNLOADS_URL).path.rstrip("/")

        for step in range(max_hops):
            host = urlparse(response.url).netloc.lower()
            response_path = urlparse(response.url).path.rstrip("/")
            if "access.redhat.com" in host and response_path.startswith(downloads_path):
                break

            soup = BeautifulSoup(response.text, "html.parser")
            form = self._select_sso_handoff_form(soup)
            if form is None:
                relay_state_target = self._extract_relay_state_target(response.url)
                if relay_state_target:
                    hops.append({"step": step + 1, "url": response.url, "action": relay_state_target, "status": "relaystate-bounce"})
                    response = self._request("get", relay_state_target, allow_redirects=True)
                    continue
                hops.append({"step": step + 1, "url": response.url, "action": None, "status": "no-form"})
                break

            action = self._attr_to_str(form.get("action")).strip()
            if not action:
                hops.append({"step": step + 1, "url": response.url, "action": None, "status": "no-action"})
                break

            method = self._attr_to_str(form.get("method"), "post").lower()
            target_url = urljoin(response.url, action)
            payload: dict[str, str] = {}
            for input_tag in form.find_all("input"):
                name = self._attr_to_str(input_tag.get("name"))
                if not name:
                    continue
                payload[name] = self._attr_to_str(input_tag.get("value"))

            hops.append(
                {
                    "step": step + 1,
                    "url": response.url,
                    "method": method,
                    "action": target_url,
                    "field_count": len(payload),
                }
            )

            # Guard against non-SSO forms that submit back to themselves (e.g. site search).
            if method == "get" and not payload and target_url.rstrip("/") == response.url.rstrip("/"):
                hops[-1]["status"] = "loop-guard"
                break

            if method == "get":
                response = self._request("get", target_url, params=payload, allow_redirects=True)
            else:
                response = self._request("post", target_url, data=payload, allow_redirects=True)

        self.last_debug["sso_handoff_hops"] = hops
        self.last_debug["sso_handoff_final_url"] = response.url
        self.last_debug["sso_handoff_final_host"] = urlparse(response.url).netloc
        return response

    @staticmethod
    def _select_sso_handoff_form(soup: BeautifulSoup):
        forms = soup.find_all("form")
        if not forms:
            return None

        # Prefer SAML relay/autosubmit forms.
        for form in forms:
            action = RedHatDownloadClient._attr_to_str(form.get("action")).lower()
            if "saml" in action or "relaystate" in action:
                return form
            if form.find("input", attrs={"name": "SAMLResponse"}) is not None:
                return form
            if form.find("input", attrs={"name": "SAMLRequest"}) is not None:
                return form
            if form.find("input", attrs={"name": "RelayState"}) is not None:
                return form
            if "services/primer/session/scribe" in action:
                return form
            if "openid-connect" in action and "login-actions" in action:
                return form

        return None

    @staticmethod
    def _select_login_form(soup: BeautifulSoup):
        forms = soup.find_all("form")
        if not forms:
            return None

        # Prefer an explicit password verification form used by Red Hat Keycloak pages.
        for form in forms:
            form_id = RedHatDownloadClient._attr_to_str(form.get("id")).lower()
            if "password" in form_id and form.get("action"):
                return form

        # Then prefer forms that post to login-actions/authenticate.
        for form in forms:
            action = RedHatDownloadClient._attr_to_str(form.get("action"))
            if "login-actions/authenticate" in action:
                return form

        # Then prefer forms containing a password field.
        for form in forms:
            if form.find("input", attrs={"name": "password"}) is not None:
                return form

        return forms[0]

    @staticmethod
    def _extract_action_from_html(html: str) -> str:
        # Fallback for pages where the action is not set directly on the chosen form.
        match = re.search(r"https://sso\.redhat\.com/auth/realms/redhat-external/login-actions/authenticate\?[^\"'\s<]+", html)
        return match.group(0) if match else ""

    def _fetch_keycloak_login_page(self) -> requests.Response:
        redirect_uri = (
            "https://access.redhat.com/services/primer/session/scribe/"
            "?redirectTo=https%3A%2F%2Faccess.redhat.com%2Fdownloads%2Fcontent%2Frhel"
        )
        params = {
            "client_id": "customer-portal",
            "redirect_uri": redirect_uri,
            "state": str(uuid.uuid4()),
            "response_mode": "fragment",
            "response_type": "code",
            "scope": "roles openid api.graphql api.ask_red_hat",
            "nonce": str(uuid.uuid4()),
        }
        auth_url = f"{self.SSO_AUTH_URL}?{urlencode(params)}"
        self.last_debug["keycloak_auth_url"] = auth_url
        response = self._request("get", auth_url, allow_redirects=True)
        self.last_debug["keycloak_login_page_url"] = response.url
        self.last_debug["keycloak_login_page_status"] = response.status_code
        self.last_debug["keycloak_login_page_bytes"] = len(response.text or "")
        return response

    def _throttle_before_request(self) -> None:
        if self.min_request_interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_monotonic
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        allow_redirects = kwargs.pop("allow_redirects", True)
        attempts_log = self.last_debug.setdefault("rate_limit_retries", [])

        for attempt in range(self.max_rate_limit_retries + 1):
            self._throttle_before_request()
            response = self.session.request(
                method=method,
                url=url,
                timeout=timeout,
                allow_redirects=allow_redirects,
                **kwargs,
            )
            self._last_request_monotonic = time.monotonic()

            if response.status_code != 429:
                response.raise_for_status()
                return response

            backoff_wait = 10.0
            attempts_log.append(
                {
                    "attempt": attempt + 1,
                    "status": response.status_code,
                    "url": url,
                    "wait_seconds": round(backoff_wait, 2),
                }
            )
            if attempt >= self.max_rate_limit_retries:
                response.raise_for_status()
            time.sleep(backoff_wait)

        raise RedHatDownloadError("Unexpected request retry flow")

    def _extract_qcow2_links(self, html: str) -> list[RedHatImageItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: dict[str, RedHatImageItem] = {}
        keywords = ("qcow2", "qcow", "kvm guest image", "guest image")
        debug = {
            "anchors_total": 0,
            "anchors_candidates": 0,
            "data_nodes_candidates": 0,
            "regex_qcow2_hits": 0,
            "accepted_candidates": 0,
            "rejected_missing_version": 0,
            "rejected_missing_keyword": 0,
        }

        def add_candidate(*, source_text: str, url: str, label: str) -> None:
            if not url:
                return
            absolute_url = urljoin(self.DOWNLOADS_URL, url.strip())
            parsed = urlparse(absolute_url)
            path = (parsed.path or "").lower()
            host = (parsed.netloc or "").lower()
            raw_label = (label or "").strip()
            lowered_label = raw_label.lower()

            if lowered_label in {"download", "build latest", "skip to main content", "skip to navigation"}:
                return
            if parsed.fragment:
                # Reject same-page anchors such as /downloads/content/rhel#masthead.
                return

            is_qcow2_artifact = ".qcow2" in path
            if not is_qcow2_artifact:
                return

            lowered = source_text.lower()
            if not any(keyword in lowered for keyword in keywords):
                debug["rejected_missing_keyword"] += 1
                return
            major_version = self._detect_major_version(source_text)
            if not major_version:
                debug["rejected_missing_version"] += 1
                return
            final_label = label.strip() or absolute_url.rsplit("/", 1)[-1]
            items[absolute_url] = RedHatImageItem(
                label=final_label,
                url=absolute_url,
                major_version=major_version,
            )
            debug["accepted_candidates"] += 1

        for anchor in soup.find_all("a", href=True):
            debug["anchors_total"] += 1
            href = self._attr_to_str(anchor.get("href")).strip()
            text = " ".join(anchor.get_text(" ", strip=True).split())
            row = anchor.find_parent(["tr", "li", "div", "section", "article"])
            row_text = " ".join(row.get_text(" ", strip=True).split()) if row else ""
            combined = f"{text} {href} {row_text}"
            debug["anchors_candidates"] += 1
            add_candidate(source_text=combined, url=href, label=text)

        # Some pages store download URLs on non-anchor elements.
        for node in soup.find_all(True):
            attrs = node.attrs or {}
            text = " ".join(node.get_text(" ", strip=True).split())
            row = node.find_parent(["tr", "li", "div", "section", "article"])
            row_text = " ".join(row.get_text(" ", strip=True).split()) if row else ""
            combined_text = f"{text} {row_text}"
            if not any(keyword in combined_text.lower() for keyword in keywords):
                continue

            for key in ("data-url", "data-download-url", "data-href", "href"):
                value = attrs.get(key)
                string_value = self._attr_to_str(value)
                if string_value.strip():
                    debug["data_nodes_candidates"] += 1
                    add_candidate(source_text=combined_text, url=string_value, label=text)

        # Fallback: scan raw HTML/script text for direct qcow2 URLs.
        for match in re.finditer(r"https?://[^\s\"'<>]+\.qcow2(?:\.[a-z0-9]+)?", html, flags=re.IGNORECASE):
            debug["regex_qcow2_hits"] += 1
            url = match.group(0)
            start = max(0, match.start() - 300)
            end = min(len(html), match.end() + 300)
            context = html[start:end]
            add_candidate(source_text=context, url=url, label=url.rsplit("/", 1)[-1])

        result = sorted(items.values(), key=lambda v: (int(v.major_version), v.label))
        debug["unique_items"] = len(result)
        debug["script_tags"] = len(soup.find_all("script"))
        self.last_debug.update(debug)
        return result

    @staticmethod
    def _detect_major_version(value: str) -> str:
        match = re.search(r"rhel[^0-9]*([0-9]{1,2})", value)
        if match:
            return match.group(1)
        match = re.search(r"\b(8|9|10)\b", value)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _resolve_filename(response: requests.Response, source_url: str) -> str:
        content_disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', content_disposition)
        if match:
            return match.group(1)
        name = source_url.rsplit("/", 1)[-1]
        return name or "rhel-image.qcow2"

    def _extract_versions_for_major(self, html: str, major: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        versions: set[str] = set()

        for option in soup.find_all("option"):
            text = " ".join(option.get_text(" ", strip=True).split())
            value = self._attr_to_str(option.get("value"))
            for source in (text, value):
                match = re.search(rf"\b{re.escape(str(major))}(?:\.\d+)?\b", source)
                if match:
                    versions.add(match.group(0))

        if not versions:
            for match in re.finditer(rf"/rhel---{re.escape(str(major))}/({re.escape(str(major))}(?:\.\d+)?)/x86_64/product-software", html):
                versions.add(match.group(1))

        return sorted(versions, key=self._version_sort_key, reverse=True)

    def _build_product_url(self, *, content_id: str, major: str, version: str) -> str:
        return f"https://access.redhat.com/downloads/content/{content_id}/ver=/rhel---{major}/{version}/x86_64/product-software"

    def _extract_iso_links_from_product_page(
        self,
        html: str,
        page_url: str,
        major: str,
        version: str,
        dvd_only: bool = False,
    ) -> list[RedHatISOItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: dict[str, RedHatISOItem] = {}
        allowed_hosts = {"access.redhat.com", "cdn.redhat.com"}

        def find_iso_name(text: str) -> str:
            match = re.search(r"([a-z0-9][a-z0-9._+-]*\.iso)\b", text or "", flags=re.IGNORECASE)
            return match.group(1) if match else ""

        def looks_like_iso_target(url: str) -> bool:
            if not url:
                return False
            absolute = urljoin(page_url, url.strip())
            parsed = urlparse(absolute)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
            if not any(allowed in host for allowed in allowed_hosts):
                return False
            if parsed.fragment:
                return False
            if ".iso" in path:
                return True
            if re.search(r"/downloads/content/\d+/", path) is not None and "/file/" in path:
                return True
            return False

        def add_item(url: str, title: str, context: str) -> None:
            absolute_url = urljoin(page_url, url.strip())
            if not looks_like_iso_target(absolute_url):
                return

            lowered = f"{title} {context}".lower()
            if "kvm guest image" in lowered:
                return
            if "iso" not in lowered and "dvd" not in lowered and ".iso" not in absolute_url.lower():
                return

            trimmed_title = " ".join((title or "").split())
            context_iso_name = find_iso_name(f"{title} {context}")
            url_iso_name = find_iso_name(absolute_url)

            if not trimmed_title:
                trimmed_title = context_iso_name or url_iso_name

            if trimmed_title.lower() in {"download now", "download"}:
                trimmed_title = context_iso_name or url_iso_name

            if not trimmed_title:
                # Drop generic buttons that do not expose a concrete ISO artifact name.
                return

            final_iso_name = find_iso_name(f"{trimmed_title} {context} {absolute_url}")
            if dvd_only:
                if not final_iso_name:
                    return
                if re.search(r"^rhel-\d+(?:\.\d+)?-x86_64-dvd\.iso$", final_iso_name, flags=re.IGNORECASE) is None:
                    return
                trimmed_title = final_iso_name

            label = f"RHEL {major} {version} - {trimmed_title}"
            items[absolute_url] = RedHatISOItem(label=label, url=absolute_url, version=version, major_version=major)

        for anchor in soup.find_all("a", href=True):
            href = self._attr_to_str(anchor.get("href")).strip()
            if not href:
                continue
            text = " ".join(anchor.get_text(" ", strip=True).split())
            row = anchor.find_parent(["article", "section", "li", "div", "tr"])
            row_text = " ".join(row.get_text(" ", strip=True).split()) if row else ""
            title = ""
            if row is not None:
                title_tag = row.find(["h2", "h3", "h4", "h5", "strong"])
                if title_tag is not None:
                    title = " ".join(title_tag.get_text(" ", strip=True).split())
            if not title:
                title = text or row_text[:140]
            add_item(href, title=title, context=row_text)

        # Some product pages render download targets in data-* attributes.
        for node in soup.find_all(True):
            attrs = node.attrs or {}
            text = " ".join(node.get_text(" ", strip=True).split())
            if not text:
                continue
            lowered_text = text.lower()
            if "iso" not in lowered_text and "dvd" not in lowered_text and "download" not in lowered_text:
                continue

            for key in ("data-url", "data-download-url", "data-href", "href"):
                value = self._attr_to_str(attrs.get(key)).strip()
                if value:
                    add_item(value, title=text[:140], context=text)

        # Fallback: capture direct ISO links in scripts/inline JSON.
        for match in re.finditer(r"https?://[^\s\"'<>]+\.iso(?:\.[a-z0-9]+)?", html, flags=re.IGNORECASE):
            iso_url = match.group(0)
            add_item(iso_url, title=iso_url.rsplit("/", 1)[-1], context=iso_url)

        return sorted(items.values(), key=lambda item: item.label)

    def _extract_version_page_urls_from_dropdown(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: set[str] = set()
        base_info = self._parse_product_software_url(page_url)
        base_content_id = self._parse_product_content_url(page_url)

        def add_candidate(value: Any, text: str = "") -> None:
            candidate = self._attr_to_str(value).strip()
            if not candidate:
                return
            absolute = urljoin(page_url, candidate)
            parsed = self._parse_product_software_url(absolute)
            if parsed:
                urls.add(absolute)
                return

            if base_content_id is None:
                return
            version_match = re.search(r"\b\d+(?:\.\d+)?\b", f"{candidate} {text}")
            if not version_match:
                return
            version_value = version_match.group(0)
            if version_value == (base_info["major"] if base_info else ""):
                return
            if "." not in version_value:
                return
            major_value = version_value.split(".", 1)[0]
            derived = self._build_product_url(
                content_id=base_content_id,
                major=major_value,
                version=version_value,
            )
            urls.add(derived)

        select = soup.find("select", id="prod_version_chosen")
        if select is None:
            select = soup.select_one("#prod_version_chosen")

        if select is not None:
            for option in select.find_all("option"):
                option_text = " ".join(option.get_text(" ", strip=True).split())
                add_candidate(option.get("value"), option_text)
                add_candidate(option.get("data-url"), option_text)
                add_candidate(option.get("data-href"), option_text)

        normalized_html = html.replace("\\/", "/")
        normalized_html = unquote(normalized_html)
        for match in re.finditer(
            r"(?:https://access\.redhat\.com)?/downloads/content/\d+/ver=/rhel---\d+/\d+(?:\.\d+)?/x86_64/product-software",
            normalized_html,
        ):
            add_candidate(match.group(0))

        if base_content_id and not urls:
            if base_info is not None:
                urls.add(
                    self._build_product_url(
                        content_id=base_info["content_id"],
                        major=base_info["major"],
                        version=base_info["version"],
                    )
                )
            else:
                return []

        return sorted(
            urls,
            key=lambda current_url: self._version_sort_key(
                (self._parse_product_software_url(current_url) or {}).get("version", "0")
            ),
            reverse=True,
        )

    def _extract_product_software_pages(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = self._attr_to_str(anchor.get("href")).strip()
            if not href:
                continue
            absolute = urljoin(self.DOWNLOADS_URL, href)
            if self._parse_product_software_url(absolute):
                urls.add(absolute)

        normalized_html = html.replace("\\/", "/")
        normalized_html = unquote(normalized_html)
        pattern = r"(?:https://access\.redhat\.com)?/downloads/content/\d+/ver=/rhel---\d+/\d+(?:\.\d+)?/x86_64/product-software"
        for match in re.finditer(pattern, normalized_html):
            candidate = match.group(0)
            absolute = urljoin(self.DOWNLOADS_URL, candidate)
            if self._parse_product_software_url(absolute):
                urls.add(absolute)

        return sorted(urls)

    @staticmethod
    def _parse_product_content_url(url: str) -> str:
        match = re.search(r"/downloads/content/(?P<content_id>\d+)(?:/|$)", url)
        return match.group("content_id") if match else ""

    @staticmethod
    def _extract_relay_state_target(url: str) -> str:
        parsed = urlparse(url)
        if "sso.redhat.com" not in (parsed.netloc or "").lower():
            return ""
        relay_state_values = parse_qs(parsed.query).get("RelayState", [])
        if not relay_state_values:
            return ""
        relay_state = relay_state_values[0].strip()
        if not relay_state:
            return ""
        return relay_state

    def _parse_product_software_url(self, url: str) -> dict[str, str] | None:
        match = re.search(r"/downloads/content/(?P<content_id>\d+)/ver=/rhel---(?P<major>\d+)/(?P<version>\d+(?:\.\d+)?)/x86_64/product-software", url)
        if not match:
            return None
        return {
            "content_id": match.group("content_id"),
            "major": match.group("major"),
            "version": match.group("version"),
        }

    @staticmethod
    def _version_sort_key(version: str) -> tuple[int, ...]:
        parts = []
        for token in str(version).split("."):
            try:
                parts.append(int(token))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    @staticmethod
    def _attr_to_str(value: Any, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        return str(value)
