from django.test import TestCase
from bs4 import BeautifulSoup

from .models import ServerConfiguration
from .services.redhat import RedHatDownloadClient


class ServerConfigurationSingletonTests(TestCase):
	def test_get_solo_creates_default_configuration(self):
		cfg = ServerConfiguration.get_solo()

		self.assertEqual(cfg.pk, ServerConfiguration.SINGLETON_PK)
		self.assertEqual(cfg.name, ServerConfiguration.DEFAULT_NAME)

	def test_singleton_update_keeps_single_row(self):
		cfg = ServerConfiguration.get_solo()
		cfg.concurrent_builds = 7
		cfg.save()

		self.assertEqual(ServerConfiguration.objects.count(), 1)
		refreshed = ServerConfiguration.get_solo()
		self.assertEqual(refreshed.concurrent_builds, 7)
		self.assertEqual(refreshed.name, ServerConfiguration.DEFAULT_NAME)

	def test_stores_and_recovers_rhn_password(self):
		cfg = ServerConfiguration.get_solo()
		cfg.rhn_username = "martinjuhl"
		cfg.set_rhn_password("super-secret")
		cfg.save()

		refreshed = ServerConfiguration.get_solo()
		self.assertEqual(refreshed.rhn_username, "martinjuhl")
		self.assertTrue(refreshed.has_rhn_password())
		self.assertEqual(refreshed.get_rhn_password(), "super-secret")
		self.assertNotEqual(refreshed.rhn_password_encrypted, "super-secret")

	def test_clear_rhn_password(self):
		cfg = ServerConfiguration.get_solo()
		cfg.set_rhn_password("to-be-cleared")
		cfg.clear_rhn_password()
		cfg.save()

		refreshed = ServerConfiguration.get_solo()
		self.assertFalse(refreshed.has_rhn_password())
		self.assertEqual(refreshed.get_rhn_password(), "")


class RedHatParserTests(TestCase):
		def test_rejects_generic_download_link_without_qcow2_artifact_url(self):
				html = """
				<table>
					<tr>
						<td>Red Hat Enterprise Linux 10.1 KVM Guest Image</td>
						<td><a href=\"/downloads/content/rhel/10/x86_64/kvm\">Download</a></td>
					</tr>
				</table>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				items = client._extract_qcow2_links(html)

				self.assertEqual(len(items), 0)

		def test_rejects_downloads_content_page_url_without_qcow2_extension(self):
				html = """
				<table>
					<tr>
						<td>RHEL 10.2 KVM Guest Image</td>
						<td><a href=\"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.2/x86_64/product-software\">Download Now</a></td>
					</tr>
				</table>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				items = client._extract_qcow2_links(html)

				self.assertEqual(len(items), 0)

		def test_extracts_embedded_qcow2_url_from_script_blob(self):
				html = """
				<script>
					const data = {
						label: "RHEL 9 KVM Guest Image",
						url: "https://example.invalid/rhel-9-kvm-guest.qcow2"
					};
				</script>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				items = client._extract_qcow2_links(html)

				self.assertEqual(len(items), 1)
				self.assertEqual(items[0].major_version, "9")
				self.assertTrue(items[0].url.endswith(".qcow2"))

		def test_select_sso_handoff_form_prefers_saml_fields(self):
				html = """
				<html><body>
					<form id="unrelated"><input name="foo" value="bar"></form>
					<form id="saml-form" action="https://access.redhat.com/saml/consume" method="post">
						<input name="SAMLResponse" value="abc">
						<input name="RelayState" value="https://access.redhat.com/downloads/content/rhel">
					</form>
				</body></html>
				"""
				soup = BeautifulSoup(html, "html.parser")

				form = RedHatDownloadClient._select_sso_handoff_form(soup)

				self.assertIsNotNone(form)
				self.assertEqual(form.get("id"), "saml-form")

		def test_select_sso_handoff_form_ignores_non_sso_forms(self):
				html = """
				<html><body>
					<form id="site-search" action="https://access.redhat.com/search/browse/search/" method="get">
						<input name="q" value="">
					</form>
				</body></html>
				"""
				soup = BeautifulSoup(html, "html.parser")

				form = RedHatDownloadClient._select_sso_handoff_form(soup)

				self.assertIsNone(form)

		def test_extract_relay_state_target_returns_redirect_url(self):
				url = "https://sso.redhat.com/auth/realms/redhat-external/protocol/saml?SAMLRequest=abc&RelayState=https%3A%2F%2Faccess.redhat.com%2Fdownloads%2Fcontent%2F146%2Fver%3D%2Frhel---10%2F10.2%2Fx86_64%2Fproduct-software"

				target = RedHatDownloadClient._extract_relay_state_target(url)

				self.assertEqual(
					target,
					"https://access.redhat.com/downloads/content/146/ver=/rhel---10/10.2/x86_64/product-software",
				)

		def test_iso_parser_uses_real_iso_name_instead_of_download_now(self):
				html = """
				<div class=\"item\">
					<h3>Download Now</h3>
					<p>Binary DVD image rhel-10.2-x86_64-dvd.iso</p>
					<a href=\"https://cdn.redhat.com/content/dist/rhel10/10.2/x86_64/dvd/rhel-10.2-x86_64-dvd.iso\">Download Now</a>
				</div>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				items = client._extract_iso_links_from_product_page(
					html,
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.2/x86_64/product-software",
					"10",
					"10.2",
				)

				self.assertEqual(len(items), 1)
				self.assertIn("rhel-10.2-x86_64-dvd.iso", items[0].label)

		def test_shared_page_extracts_qcow2_and_dvd_without_generic_download_now(self):
				html = """
				<div class=\"card\">
					<h3>Download Now</h3>
					<p>RHEL 10.2 KVM Guest Image rhel-10.2-x86_64-kvm.qcow2</p>
					<a href=\"https://cdn.redhat.com/files/rhel-10.2-x86_64-kvm.qcow2\">Download Now</a>
				</div>
				<div class=\"card\">
					<h3>Download Now</h3>
					<p>RHEL 10.2 DVD ISO rhel-10.2-x86_64-dvd.iso</p>
					<a href=\"https://cdn.redhat.com/files/rhel-10.2-x86_64-dvd.iso\">Download Now</a>
				</div>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				qcow2_items = client._extract_qcow2_links(html)
				iso_items = client._extract_iso_links_from_product_page(
					html,
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.2/x86_64/product-software",
					"10",
					"10.2",
					dvd_only=True,
				)

				self.assertEqual(len(qcow2_items), 1)
				self.assertTrue(qcow2_items[0].url.endswith(".qcow2"))
				self.assertEqual(len(iso_items), 1)
				self.assertIn("rhel-10.2-x86_64-dvd.iso", iso_items[0].label)

		def test_extract_product_pages_from_escaped_and_relative_urls(self):
				html = """
				<script>
					const links = [
						\"https:\\/\\/access.redhat.com\\/downloads\\/content\\/479\\/ver=\\/rhel---9\\/9.6\\/x86_64\\/product-software\",
						\"/downloads/content/479/ver=/rhel---8/8.10/x86_64/product-software\"
					];
				</script>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				pages = client._extract_product_software_pages(html)

				self.assertIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---9/9.6/x86_64/product-software",
					pages,
				)
				self.assertIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---8/8.10/x86_64/product-software",
					pages,
				)

		def test_extract_versions_from_prod_version_dropdown(self):
				html = """
				<select id="prod_version_chosen">
					<option value="10.2">10.2</option>
					<option value="10.1">10.1</option>
					<option value="10.0">10.0</option>
					<option value="10">10</option>
				</select>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				urls = client._extract_version_page_urls_from_dropdown(
					html,
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.2/x86_64/product-software",
				)

				self.assertEqual(len(urls), 3)
				self.assertIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.0/x86_64/product-software",
					urls,
				)
				self.assertNotIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10/x86_64/product-software",
					urls,
				)

		def test_extract_versions_from_prod_version_dropdown_accepts_other_major_versions(self):
				html = """
				<select id="prod_version_chosen">
					<option value="7.9">7.9</option>
					<option value="7.8">7.8</option>
				</select>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				urls = client._extract_version_page_urls_from_dropdown(
					html,
					"https://access.redhat.com/downloads/content/287/ver=/rhel---7/7.9/x86_64/product-software",
				)

				self.assertEqual(len(urls), 2)
				self.assertIn(
					"https://access.redhat.com/downloads/content/287/ver=/rhel---7/7.9/x86_64/product-software",
					urls,
				)
				self.assertIn(
					"https://access.redhat.com/downloads/content/287/ver=/rhel---7/7.8/x86_64/product-software",
					urls,
				)

		def test_extract_versions_from_root_content_page_dropdown(self):
				html = """
				<select id="prod_version_chosen">
					<option value="10.2">10.2 (latest)</option>
					<option value="10.1">10.1</option>
				</select>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				urls = client._extract_version_page_urls_from_dropdown(
					html,
					"https://access.redhat.com/downloads/content/479/",
				)

				self.assertEqual(len(urls), 2)
				self.assertIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.2/x86_64/product-software",
					urls,
				)
				self.assertIn(
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.1/x86_64/product-software",
					urls,
				)

		def test_iso_parser_dvd_only_filters_non_dvd_images(self):
				html = """
				<div>
					<a href="https://cdn.redhat.com/files/rhel-10.1-x86_64-boot.iso">Boot</a>
					<a href="https://cdn.redhat.com/files/rhel-10.1-x86_64-dvd.iso">DVD</a>
					<a href="https://cdn.redhat.com/files/virtio-win-1.9.52.iso">virtio</a>
				</div>
				"""

				client = RedHatDownloadClient(username="u", password="p")
				items = client._extract_iso_links_from_product_page(
					html,
					"https://access.redhat.com/downloads/content/479/ver=/rhel---10/10.1/x86_64/product-software",
					"10",
					"10.1",
					dvd_only=True,
				)

				self.assertEqual(len(items), 1)
				self.assertIn("rhel-10.1-x86_64-dvd.iso", items[0].label)
