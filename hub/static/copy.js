// Globaler Copy-Handler fuer Dashboard und Setup-Seite.
// Unterstuetzt drei Varianten:
//   <button class="copy-btn" data-copy-target="elem-id">…</button>
//   <button class="copy-btn" data-copy-text="literal text">…</button>
//   <code class="copyable" data-copy="literal text">…</code>
(function () {
    "use strict";
    // Marker fuer alte Inline-Listener, damit sie sich selbst deaktivieren.
    window.__hotsportCopyJsLoaded = true;

    function flashLabel(el, label) {
        var orig = el.textContent;
        el.textContent = label;
        el.classList.add("copied");
        setTimeout(function () {
            // Sonderfall: Token-Display soll nach "Kopiert" wieder maskiert
            // sein, falls der Operator den Token nicht explizit enthuellt hat.
            if (el.id === "token-display" && el.dataset.revealed !== "1") {
                el.textContent = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";
            } else {
                el.textContent = orig;
            }
            el.classList.remove("copied");
        }, 1500);
    }

    async function copy(text, feedbackEl) {
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            if (feedbackEl) flashLabel(feedbackEl, "Kopiert \u2713");
        } catch (err) {
            if (feedbackEl) feedbackEl.textContent = "Fehler – manuell kopieren";
        }
    }

    document.addEventListener("click", function (e) {
        var btn = e.target.closest(".copy-btn");
        if (btn) {
            var literal = btn.getAttribute("data-copy-text");
            if (literal) {
                copy(literal, btn);
                return;
            }
            var targetId = btn.getAttribute("data-copy-target");
            if (targetId) {
                var el = document.getElementById(targetId);
                if (el) {
                    copy(el.innerText.replace(/\n+$/, ""), btn);
                }
            }
            return;
        }

        var copyable = e.target.closest(".copyable");
        if (copyable && copyable.dataset.copy) {
            copy(copyable.dataset.copy, copyable);
        }
    });
})();
