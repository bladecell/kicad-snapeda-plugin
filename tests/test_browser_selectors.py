from __future__ import annotations

import asyncio

try:
    from camoufox.async_api import AsyncCamoufox
except ModuleNotFoundError:
    if __name__ != "__main__":
        import pytest

        pytest.skip(
            "Camoufox is only installed in the browser environment",
            allow_module_level=True,
        )
    raise

from snapeda_plugin import browser

_EXPORT_FIXTURE = """
<a id="open-export" href="#" data-choice="part-export">
  <i class="download-icon">&#xf019;</i>
  <span>Download Symbol and Footprint</span>
</a>
<a href="#" data-choice="footprint">Download Footprint</a>
<a class="s-btn s-btn-medium s-btn-modal download-static-btn"
   data-format="KiCad" href="https://s3.amazonaws.com/snapeda/cpl/Common-Parts-Library-KiCad.zip"
   style="display:none">KiCad</a>
<footer><a href="/footer/kicad">KiCad</a><a href="/footer/download">Download</a></footer>
<section id="format-modal">
  <a class="s-btn s-btn-medium s-btn-modal download-static-btn"
     data-format="KiCad" href="#" data-choice="part-kicad">KiCad</a>
</section>
<section id="version-modal" hidden>
  <a class="s-btn s-btn-modal" href="#" data-choice="v6">V6 &amp; Later</a>
</section>
<section id="download-modal" hidden>
  <a class="s-btn s-btn-modal" href="#" data-choice="part-download">Download</a>
</section>
<output id="chosen"></output>
<script>
  document.querySelectorAll('a[data-choice]').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      document.querySelector('#chosen').textContent = link.dataset.choice;
      if (link.dataset.choice === 'part-kicad') {
        document.querySelector('#format-modal').hidden = true;
        document.querySelector('#version-modal').hidden = false;
      } else if (link.dataset.choice === 'v6') {
        document.querySelector('#version-modal').hidden = true;
        document.querySelector('#download-modal').hidden = false;
      }
    });
  });
</script>
"""


def test_export_flow_clicks_visible_part_modal_controls_not_cpl_or_footer() -> None:
    async def run() -> None:
        async with AsyncCamoufox(headless=True) as camoufox:
            page = await camoufox.new_page()
            await page.set_content(_EXPORT_FIXTURE)

            export_link = browser._part_export_download_link(page)
            assert await export_link.count() == 1
            await browser._click_unique_visible(export_link, "part export link")
            assert await page.locator("#chosen").inner_text() == "part-export"
            await browser._click_modal_option(page, "KiCad")
            assert await page.locator("#chosen").inner_text() == "part-kicad"

            await browser._click_modal_option(page, "V6 & Later")
            assert await page.locator("#chosen").inner_text() == "v6"

            await browser._click_modal_option(page, "Download")
            assert await page.locator("#chosen").inner_text() == "part-download"

    asyncio.run(run())


if __name__ == "__main__":
    test_export_flow_clicks_visible_part_modal_controls_not_cpl_or_footer()
