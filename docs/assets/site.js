"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const search = document.getElementById("course-search");
  const rows = Array.from(document.querySelectorAll("#experiment-list-items li"));
  const empty = document.getElementById("empty-search");
  const help = document.getElementById("search-help");
  if (search && rows.length) {
    const filter = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let visible = 0;
      rows.forEach((row) => {
        const match = !query || row.dataset.search.toLocaleLowerCase().includes(query);
        row.hidden = !match;
        if (match) visible += 1;
      });
      if (empty) empty.hidden = visible !== 0;
      if (help) help.textContent = query ? `找到 ${visible} 项。清空搜索框可恢复完整目录。` : "搜索只筛实验名称和主题；清空后显示完整目录。";
    };
    search.addEventListener("input", filter);
    search.addEventListener("search", filter);
  }
  document.querySelectorAll("[data-print]").forEach((link) => {
    link.addEventListener("click", (event) => { event.preventDefault(); window.print(); });
  });
  document.querySelectorAll(".prose pre > code").forEach((code) => {
    const pre = code.parentElement;
    const text = code.textContent;
    if (!text.trim() || pre.dataset.copy === "false" || (!text.trim().includes("\n") && text.trim().length < 25)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-command";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const icon = document.createElementNS(svg.namespaceURI, "path");
    icon.setAttribute("d", "M9 7V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2M5 9h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2Z");
    svg.appendChild(icon);
    const status = document.createElement("span");
    status.setAttribute("aria-live", "polite");
    button.append(svg, status);
    const label = (message) => {
      status.textContent = message === "复制代码" ? "" : message;
      button.title = message;
      button.setAttribute("aria-label", message);
    };
    label("复制代码");
    let reset;
    button.addEventListener("click", async () => {
      window.clearTimeout(reset);
      try {
        await navigator.clipboard.writeText(text);
        label("已复制");
      } catch (_) {
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        let copied = false;
        try { copied = document.execCommand("copy"); } catch (_) { /* Retain the selection for manual copying. */ }
        if (copied) selection.removeAllRanges();
        label(copied ? "已复制" : "已选中，按 Ctrl+C");
      }
      reset = window.setTimeout(() => label("复制代码"), 2400);
    });
    pre.classList.add("has-copy");
    pre.appendChild(button);
  });
});
