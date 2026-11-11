"""Ferramenta de navegador para o agente (Playwright).

O Claude vê a página como texto + uma lista numerada de elementos clicáveis/preenchíveis
(campos, botões, links), inclusive dentro de frames e janelas pop-up, e age por esse número.
"""

import base64
import os

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

OCULTO = os.getenv("AGENTE_NAVEGADOR_OCULTO") == "1"
CHROMIUM = os.getenv("AGENTE_CHROMIUM_PATH") or None
CANAL = os.getenv("AGENTE_NAVEGADOR_CANAL") or None  # "msedge" ou "chrome": usa o já instalado
MAX_TEXTO = 15_000

# Marca os elementos visíveis com um número (data-agente-ref) e devolve a descrição deles.
_JS_ELEMENTOS = r"""
(inicio) => {
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],[role=menuitem],' +
              '[role=tab],[role=option],[onclick],[contenteditable=true]';
  const out = []; let i = inicio;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.type === 'hidden' || el.disabled) continue;
    el.setAttribute('data-agente-ref', String(i));
    let rotulo = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) rotulo = l.innerText; }
    const tag = el.tagName.toLowerCase();
    const ehCampo = tag === 'input' || tag === 'textarea' || tag === 'select';
    const texto = ehCampo ? '' : (el.innerText || '');
    const desc = (rotulo || texto || el.getAttribute('aria-label') || el.title || el.alt ||
                  el.placeholder || (el.type === 'submit' || el.type === 'button' ? el.value : '') || '')
                  .trim().replace(/\s+/g, ' ').slice(0, 80);
    const item = {ref: i, tag, tipo: el.type || '', desc, nome: el.name || el.id || ''};
    if (ehCampo && el.type !== 'password' && tag !== 'select') item.valor = (el.value || '').slice(0, 60);
    if (tag === 'select') item.opcoes = [...el.options].slice(0, 30).map(o => o.text.trim());
    out.push(item); i++;
  }
  return out;
}
"""


class Navegador:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self.pagina = None
        self._refs: dict[int, object] = {}  # ref -> frame onde o elemento está

    # ----- ciclo de vida -----
    def _garantir(self) -> None:
        if self.pagina and not self.pagina.is_closed():
            return
        if not self._pw:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=OCULTO, executable_path=CHROMIUM, channel=None if CHROMIUM else CANAL)
            contexto = self._browser.new_context(viewport={"width": 1366, "height": 850})
            contexto.on("page", self._nova_janela)  # pop-ups viram a página ativa
            self.pagina = contexto.new_page()
        else:
            abertas = [p for p in self._browser.contexts[0].pages if not p.is_closed()]
            self.pagina = abertas[-1] if abertas else self._browser.contexts[0].new_page()

    def _nova_janela(self, pagina) -> None:
        self.pagina = pagina
        pagina.on("close", lambda _: self._voltar_janela())

    def _voltar_janela(self) -> None:
        abertas = [p for p in self._browser.contexts[0].pages if not p.is_closed()]
        if abertas:
            self.pagina = abertas[-1]

    def fechar(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self.pagina = None

    def _esperar(self) -> None:
        try:
            self.pagina.wait_for_load_state("domcontentloaded", timeout=10_000)
            self.pagina.wait_for_load_state("networkidle", timeout=5_000)
        except PWError:
            pass

    # ----- leitura -----
    def ver(self) -> str:
        """Texto visível + lista numerada de elementos de todas as frames da janela ativa."""
        self._garantir()
        self.pagina.wait_for_timeout(500)  # dá tempo de pop-ups abrirem e telas reagirem
        self._esperar()
        self._refs = {}
        partes = [f"Janela ativa: {self.pagina.title()!r} — {self.pagina.url}"]
        n_janelas = len([p for p in self.pagina.context.pages if not p.is_closed()])
        if n_janelas > 1:
            partes.append(f"({n_janelas} janelas abertas; você está na mais recente)")
        proximo = 1
        for i, frame in enumerate(self.pagina.frames):
            try:
                texto = frame.inner_text("body", timeout=3_000).strip()
                elementos = frame.evaluate(_JS_ELEMENTOS, proximo)
            except PWError:
                continue
            if not texto and not elementos:
                continue
            nome = "principal" if i == 0 else f"frame {frame.name or i} ({frame.url})"
            partes.append(f"\n=== {nome} ===\n{texto[:4000]}")
            if elementos:
                partes.append("--- elementos ---")
                for e in elementos:
                    self._refs[e["ref"]] = frame
                    extra = f" valor={e['valor']!r}" if e.get("valor") else ""
                    if e.get("opcoes"):
                        extra += f" opções={e['opcoes']}"
                    tipo = f"[{e['tipo']}]" if e["tipo"] and e["tipo"] != e["tag"] else ""
                    partes.append(f"[{e['ref']}] {e['tag']}{tipo} {e['desc']!r} nome={e['nome']!r}{extra}")
                proximo = elementos[-1]["ref"] + 1
        texto = "\n".join(partes)
        return texto if len(texto) <= MAX_TEXTO else texto[:MAX_TEXTO] + "\n...[página cortada]"

    def captura(self) -> str:
        """Captura de tela da janela ativa em PNG (base64)."""
        self._garantir()
        return base64.b64encode(self.pagina.screenshot(type="png")).decode()

    # ----- ações -----
    def _el(self, ref: int):
        frame = self._refs.get(int(ref))
        if frame is None:
            raise ValueError(f"elemento [{ref}] não existe; chame navegador_ver para atualizar a lista")
        return frame.locator(f'[data-agente-ref="{int(ref)}"]').first

    def abrir(self, url: str) -> str:
        self._garantir()
        self.pagina.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return self.ver()

    def clicar(self, ref: int) -> str:
        self._el(ref).click(timeout=10_000)
        return self.ver()

    def digitar(self, ref: int, texto: str, enter: bool = False) -> str:
        el = self._el(ref)
        el.fill(texto, timeout=10_000)
        if enter:
            el.press("Enter")
        return self.ver()

    def selecionar(self, ref: int, opcao: str) -> str:
        self._el(ref).select_option(label=opcao, timeout=10_000)
        return self.ver()

    def tecla(self, tecla: str) -> str:
        self._garantir()
        self.pagina.keyboard.press(tecla)
        return self.ver()
