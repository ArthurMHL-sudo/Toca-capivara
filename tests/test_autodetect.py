"""Testes da deteção automática do link ao colar (browser real + backend simulado).

Uso:   python tests/test_autodetect.py
       LYRA_INDEX=/caminho/outro/index.html python tests/test_autodetect.py   (testar outra versão)
Requer: pip install playwright && playwright install chromium
"""
import json
import os
import sys
import traceback
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

srv = mock_server.serve(0)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
YT = "https://www.youtube.com/watch?v=abc123"
YT2 = "https://youtu.be/second"


def http(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    return json.loads(urllib.request.urlopen(req).read())


def info_calls():
    return [c["url"] for c in http("/__log") if c["path"] == "/api/info"]


def paste(page, text):
    """Ctrl+V real no campo (gera um evento 'paste' verdadeiro)."""
    page.evaluate("t => navigator.clipboard.writeText(t)", text)
    page.focus("#url")
    page.keyboard.press("Control+A")
    page.keyboard.press("Control+V")


def card_visible(page, timeout=3000):
    page.wait_for_selector("#card", state="visible", timeout=timeout)


def settle(page, ms=500):
    page.wait_for_timeout(ms)


def msg(page):
    return page.inner_text("#msg")


# ---------------------------------------------------------------- testes
def t_paste_url_triggers_preview(page):
    paste(page, YT)
    card_visible(page)
    assert info_calls() == [YT], info_calls()
    assert page.input_value("#url") == YT
    assert "Vídeo de teste" in page.inner_text("#title")
    assert [o for o in page.eval_on_selector_all("#quality option", "e=>e.map(x=>x.value)")] == ["best", "1080", "720", "360", "audio"]


def t_paste_text_with_link_extracts_it(page):
    paste(page, f"Vê isto: {YT}, é fixe!")
    card_visible(page)
    assert info_calls() == [YT], info_calls()
    assert page.input_value("#url") == YT


def t_trailing_punctuation_is_stripped(page):
    paste(page, f"(link: {YT}).")
    card_visible(page)
    assert info_calls() == [YT], info_calls()


def t_paste_plain_text_does_nothing(page):
    paste(page, "olá, isto não é um link")
    settle(page)
    assert info_calls() == []
    assert page.eval_on_selector("#card", "e=>getComputedStyle(e).display") == "none"
    assert "err" not in (page.get_attribute("#msg", "class") or "")


def t_invalid_link_is_ignored_on_paste(page):
    for bad in ("https://", "https://semponto", "ftp://x.com/v"):
        paste(page, bad)
        settle(page, 300)
    assert info_calls() == [], info_calls()


def t_same_link_pasted_twice_is_deduped(page):
    paste(page, YT)
    card_visible(page)
    paste(page, YT)
    settle(page)
    assert info_calls() == [YT], info_calls()


def t_new_link_replaces_old_and_stale_response_is_ignored(page):
    slow = "https://slow.example.com/v1"
    paste(page, slow)             # resposta demora 1,5 s
    settle(page, 200)
    paste(page, YT2)              # entretanto cola outro
    card_visible(page)
    page.wait_for_timeout(1800)   # a resposta lenta já chegou ao browser
    title = page.inner_text("#title")
    assert YT2 in title and "slow" not in title, title
    assert page.input_value("#url") == YT2
    assert page.is_enabled("#go")


def t_manual_button_forces_preview_even_if_same_link(page):
    paste(page, YT)
    card_visible(page)
    page.click("#go")
    page.wait_for_function("() => document.getElementById('go').disabled === false")
    settle(page, 200)
    assert info_calls() == [YT, YT], info_calls()


def t_manual_button_with_empty_field(page):
    page.click("#go")
    assert "Cola primeiro um link" in msg(page)
    assert info_calls() == []


def t_manual_button_invalid_link_message(page):
    page.fill("#url", "https://semponto")
    page.click("#go")
    assert "não parece um link válido" in msg(page)
    assert info_calls() == []


def t_typing_does_not_fire_until_commit(page):
    page.click("#url")
    page.keyboard.type(YT, delay=15)   # tecla a tecla
    settle(page, 600)
    assert info_calls() == [], f"não devia pedir enquanto se escreve: {info_calls()}"
    page.keyboard.press("Enter")
    card_visible(page)
    settle(page, 300)
    assert info_calls() == [YT], info_calls()   # Enter + change => um único pedido


def t_typed_then_blur_triggers(page):
    page.click("#url")
    page.keyboard.type(YT2)
    page.click("h1")                   # sai do campo => evento change
    card_visible(page)
    assert info_calls() == [YT2], info_calls()


def t_autofill_or_mobile_input_without_inputtype(page):
    page.evaluate("""u => { const i = document.getElementById('url'); i.value = u;
        i.dispatchEvent(new Event('input', {bubbles: true})); }""", YT)
    card_visible(page)
    assert info_calls() == [YT], info_calls()


def t_drop_input_type(page):
    page.evaluate("""u => { const i = document.getElementById('url'); i.value = u;
        i.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertFromDrop'})); }""", YT)
    card_visible(page)
    assert info_calls() == [YT], info_calls()


def t_failure_shows_error_and_repaste_retries(page):
    bad = "https://fail.example.com/x"
    paste(page, bad)
    page.wait_for_function("() => document.getElementById('msg').classList.contains('err')")
    assert "simulado" in msg(page)
    assert page.is_enabled("#go")
    paste(page, bad)
    settle(page, 400)
    assert info_calls() == [bad, bad], info_calls()


def t_multiple_items_notice(page):
    paste(page, "https://multi.example.com/playlist")
    card_visible(page)
    assert "vários itens" in msg(page)


def t_paste_button_uses_clipboard(page):
    page.evaluate("t => navigator.clipboard.writeText(t)", f"olha: {YT2}")
    page.click("#paste")
    card_visible(page)
    assert info_calls() == [YT2], info_calls()
    assert page.input_value("#url") == YT2


def t_paste_button_forces_even_if_same(page):
    paste(page, YT)
    card_visible(page)
    page.evaluate("t => navigator.clipboard.writeText(t)", YT)
    page.click("#paste")
    settle(page, 400)
    assert info_calls() == [YT, YT], info_calls()


def t_no_auto_preview_during_download(page):
    paste(page, YT)
    card_visible(page)
    page.click("#dl")
    page.wait_for_function("() => document.getElementById('pinfo').textContent.includes('%')")
    paste(page, YT2)                      # cola outro link a meio da descarga
    settle(page, 500)
    assert info_calls() == [YT], info_calls()
    assert page.eval_on_selector("#card", "e=>getComputedStyle(e).display") == "block", "o cartão/progresso não pode desaparecer"
    assert "descarga em curso" in msg(page)
    page.click("#stop")                   # limpeza
    page.wait_for_function("() => document.getElementById('msg').textContent.includes('cancelada')")
    # depois de cancelar, voltar a colar já funciona
    paste(page, YT2)
    settle(page, 500)
    assert info_calls() == [YT, YT2], info_calls()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t_")]


def main():
    passed = failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for t in TESTS:
            http("/__reset", "POST")
            ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"], base_url=BASE)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(BASE + "/")
            page.click("#start")  # passa do ecrã inicial para a aplicação
            try:
                t(page)
                assert not errors, f"erros JS na página: {errors}"
                print(f"  PASS  {t.__name__}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL  {t.__name__}: {e.__class__.__name__}: {str(e)[:200]}")
                if os.environ.get("VERBOSE"):
                    traceback.print_exc()
                failed += 1
            ctx.close()
        browser.close()
    print(f"\n{passed} passaram, {failed} falharam ({passed + failed} testes)  [{os.path.basename(os.path.dirname(mock_server.INDEX)) or 'index'}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
