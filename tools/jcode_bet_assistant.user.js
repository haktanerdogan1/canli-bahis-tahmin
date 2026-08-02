// ==UserScript==
// @name         JCode Bahis Yardımcısı
// @namespace    https://web-production-f1dba.up.railway.app/
// @version      1.2.0
// @description  JCode sinyalindeki maci acar ve marketi vurgular; bahsi gondermez.
// @match        https://inagaming696.com/*
// @grant        GM_xmlhttpRequest
// @connect      web-production-f1dba.up.railway.app
// @inject-into  content
// ==/UserScript==

(() => {
  "use strict";

  const API_URL = "https://web-production-f1dba.up.railway.app/api/bet-assistant/latest";
  const TOKEN_KEY = "jcode-bet-assistant-token";
  const POLL_MS = 30_000;
  const NAVIGATION_KEY = "jcode-bet-assistant-last-navigation";

  const normalize = (value) => String(value || "")
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("tr-TR")
    .replace(/\b(fc|fk|sk|sc|afc|cf)\b/g, " ")
    .replace(/[^a-z0-9\u0131öüşçğ]+/g, " ").trim();

  function roots(root = document) {
    const found = [root];
    for (let i = 0; i < found.length; i += 1) {
      for (const node of found[i].querySelectorAll("*")) {
        if (node.shadowRoot) found.push(node.shadowRoot);
      }
    }
    return found;
  }

  const all = (selector) => roots().flatMap((root) => [...root.querySelectorAll(selector)]);
  const text = (element) => normalize(element?.textContent);

  function teamMatches(expected, actual) {
    const a = normalize(expected);
    const b = normalize(actual);
    return a === b || a.includes(b) || b.includes(a);
  }

  function parseMarket(value) {
    const match = String(value || "").match(/^(\u0130lk Yar\u0131|Ma\u00e7 Sonu)\s+([0-9]+(?:\.[0-9]+)?)\s+(\u00dcst|Alt)$/i);
    return match && { period: match[1], line: match[2], side: match[3] };
  }

  function panel() {
    let box = document.getElementById("jcode-bet-assistant");
    if (box) return box;
    box = document.createElement("div");
    box.id = "jcode-bet-assistant";
    box.style.cssText = "position:fixed;right:16px;top:90px;z-index:2147483647;width:320px;padding:14px;border-radius:10px;background:#111827;color:#fff;font:13px/1.45 system-ui;box-shadow:0 8px 30px #0008;border:1px solid #22d3ee";
    box.innerHTML = '<b style="color:#22d3ee">JCode Yardımcı</b><div data-status style="margin-top:7px">Başlatılıyor…</div>';
    document.body.appendChild(box);
    return box;
  }

  function status(message, color = "#fff") {
    const target = panel().querySelector("[data-status]");
    target.style.color = color;
    target.textContent = message;
  }

  function getToken() {
    let token = localStorage.getItem(TOKEN_KEY);
    if (!token) {
      token = window.prompt("Railway BET_ASSISTANT_TOKEN değerini girin:")?.trim();
      if (token) localStorage.setItem(TOKEN_KEY, token);
    }
    return token;
  }

  function fetchSignal(token) {
    return new Promise((resolve, reject) => GM_xmlhttpRequest({
      method: "GET",
      url: API_URL,
      headers: {
        "Authorization": `Bearer ${token}`,
        "X-Bet-Assistant-Token": token,
      },
      timeout: 12_000,
      onload: (response) => {
        try {
          const payload = JSON.parse(response.responseText);
          if (response.status !== 200) throw new Error(payload.error || `API ${response.status}`);
          resolve(payload);
        } catch (error) { reject(error); }
      },
      onerror: () => reject(new Error("API bağlantı hatası")),
      ontimeout: () => reject(new Error("API zaman aşımı")),
    }));
  }

  function clearHighlight() {
    for (const element of all(".jcode-recommended")) {
      element.classList.remove("jcode-recommended");
      element.style.removeProperty("outline");
      element.style.removeProperty("box-shadow");
    }
  }

  const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

  function cardFor(signal) {
    for (const root of roots()) {
      for (const card of root.querySelectorAll(".lv_sideBar_card__match")) {
        const teams = [...card.querySelectorAll(".lv_sideBar_card__team-name")]
          .map((element) => element.textContent.trim());
        if (teams.some((team) => teamMatches(signal.home_team, team)) &&
            teams.some((team) => teamMatches(signal.away_team, team))) return card;
      }
    }
    return null;
  }

  function openCard(signal) {
    const card = cardFor(signal);
    if (!card) return false;
    const key = `${signal.match_id}:${window.location.href}`;
    const previous = JSON.parse(localStorage.getItem(NAVIGATION_KEY) || "null");
    if (previous?.key === key && Date.now() - previous.at < 20_000) return true;
    localStorage.setItem(NAVIGATION_KEY, JSON.stringify({ key, at: Date.now() }));
    status(`Maç bulundu, açılıyor: ${signal.home_team} - ${signal.away_team}`, "#67e8f9");
    (card.querySelector(".lv_sideBar_card__match_wrap") || card).click();
    window.setTimeout(refresh, 2_500);
    return true;
  }

  async function searchAndOpen(signal) {
    if (openCard(signal)) return true;
    const searchIcon = all(".lv_topNav_right_wrap .sport_front_icon-search")[0];
    if (!searchIcon) return false;
    searchIcon.closest(".lv_topNav_item_wrapper")?.click();

    let input = null;
    for (let attempt = 0; attempt < 12 && !input; attempt += 1) {
      await wait(250);
      input = all('[data-testid="search-input-container"] input, .lv_input_wrapper input')[0];
    }
    if (!input) return false;
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, signal.home_team);
    input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: signal.home_team }));
    input.dispatchEvent(new Event("change", { bubbles: true }));

    status(`Bahis sitesinde aranıyor: ${signal.home_team}`, "#67e8f9");
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await wait(350);
      if (openCard(signal)) return true;
    }
    return false;
  }

  function highlight(signal) {
    clearHighlight();
    const currentTeams = all(".lv_liveInfo .lv_team_name_text, [data-testid=live-info] .lv_team_name_text")
      .map((element) => element.textContent.trim());
    const hasHome = currentTeams.some((team) => teamMatches(signal.home_team, team));
    const hasAway = currentTeams.some((team) => teamMatches(signal.away_team, team));
    if (!hasHome || !hasAway) {
      status(`Önerilen maç aranıyor: ${signal.home_team} - ${signal.away_team}`, "#fbbf24");
      searchAndOpen(signal).then((opened) => {
        if (!opened) status(`Maç otomatik bulunamadı: ${signal.home_team} - ${signal.away_team}`, "#fb7185");
      });
      return;
    }

    const wanted = parseMarket(signal.market);
    if (!wanted) {
      status(`Desteklenmeyen market: ${signal.market}`, "#fb7185");
      return;
    }
    const headers = all(".dg_lv_stake_container_header_title");
    const totalGoalHeaders = headers.filter((header) => text(header).includes("toplam gol alt ust"));
    for (const header of totalGoalHeaders) {
      const container = header.closest(".dg_lv_stake_container") || header.parentElement?.parentElement?.parentElement;
      if (!container) continue;
      const candidates = [...container.querySelectorAll(".dg_lv_stake")];
      const selected = candidates.find((candidate) => {
        const side = candidate.querySelector(".dg_lv_stake_arg_name")?.textContent.trim();
        const line = candidate.querySelector(".dg_lv_stake_arg")?.textContent.trim();
        return normalize(side) === normalize(wanted.side) && line === wanted.line;
      });
      if (selected) {
        selected.classList.add("jcode-recommended");
        selected.style.setProperty("outline", "4px solid #22d3ee", "important");
        selected.style.setProperty("box-shadow", "0 0 22px #22d3ee", "important");
        selected.scrollIntoView({ behavior: "smooth", block: "center" });
        status(`Eşleşti: ${signal.home_team} - ${signal.away_team} | ${signal.market}. Seçim vurgulandı; kontrol edip siz tıklayın.`, "#86efac");
        return;
      }
    }
    status(`Maç eşleşti fakat market bulunamadı: ${signal.market}`, "#fbbf24");
  }

  async function refresh() {
    const token = getToken();
    if (!token) return status("Anahtar girilmedi; yardımcı durdu.", "#fb7185");
    try {
      const response = await fetchSignal(token);
      if (!response.data) return status("Henüz açık sinyal yok.", "#cbd5e1");
      highlight(response.data);
    } catch (error) {
      if (String(error.message).includes("eslesmiyor")) localStorage.removeItem(TOKEN_KEY);
      status(error.message, "#fb7185");
    }
  }

  panel();
  refresh();
  window.setInterval(refresh, POLL_MS);
})();
