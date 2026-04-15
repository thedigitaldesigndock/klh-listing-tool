/* KLH Dashboard — frontend workflow
 *
 * Flow:
 *   1. Fetch /api/config and /api/products on load.
 *   2. Render the product grid from catalog.tiles.
 *   3. User picks a tile → workflow panel opens.
 *   4. "Scan ONE/TWO" → GET /api/match → render one row per matched pair,
 *      then hide the product grid to focus on the workflow.
 *   5. Per row: POST /api/preview → populate editable title + best-offer
 *      preview (pure, no eBay round-trip).
 *   6. Per row: Render mockup → POST /api/mockup (inline thumb).
 *   7. Per row: Verify → POST /api/list { verify_only: true }.
 *   8. Per row: List live → POST /api/list { verify_only: false, confirm: true }.
 *   9. Bulk "List all live" walks every ready row with one confirmation.
 *
 * Global state:
 *   - state.frameMode flips every tile between mount and frame product.
 *   - state.selectedProduct is the currently-selected product view.
 *   - state.rows is the list of workflow rows after /api/match.
 */

(() => {
  "use strict";

  // ---- State ------------------------------------------------------- //
  const state = {
    catalog: null,
    frameMode: false,
    mockupOnly: false,
    selectedLayout: null,
    selectedProduct: null,
    rows: [],
    busy: false,
  };

  // ---- DOM handles ------------------------------------------------- //
  const $catalog        = document.getElementById("catalog");
  const $grid           = document.getElementById("product-grid");
  const $frameToggle    = document.getElementById("frame-toggle");   // legacy checkbox (may be null)
  const $frameSegment   = document.getElementById("frame-segment");
  const $configBanner   = document.getElementById("config-banner");
  const $productCount   = document.getElementById("product-count");
  const $tileCount      = document.getElementById("tile-count");

  const $workflow       = document.getElementById("workflow");
  const $backBtn        = document.getElementById("back-btn");
  const $productLabel   = document.getElementById("workflow-product-label");
  const $productKeyEl   = document.getElementById("workflow-product-key");
  const $defaultPrice   = document.getElementById("workflow-default-price");
  const $scanBtn        = document.getElementById("scan-btn");
  const $renderAllBtn   = document.getElementById("render-all-btn");
  const $downloadAllBtn = document.getElementById("download-all-btn");
  const $listAllBtn     = document.getElementById("list-all-btn");
  const $listAllScheduledBtn = document.getElementById("list-all-scheduled-btn");
  const $scheduleInput  = document.getElementById("schedule-input");
  const $scanStatus     = document.getElementById("scan-status");
  const $rowsTable      = document.getElementById("rows-table");
  const $rowsBody       = document.getElementById("rows-body");
  const $unmatched      = document.getElementById("unmatched-details");
  const $unmatchedBody  = document.getElementById("unmatched-body");

  const $mockupToggle   = document.getElementById("mockup-only-toggle");
  const $workflowHeading = document.getElementById("workflow-heading");

  const $lightbox       = document.getElementById("lightbox");
  const $lightboxImg    = document.getElementById("lightbox-img");
  const $lightboxClose  = document.getElementById("lightbox-close");

  const $bulkPriceInput = document.getElementById("bulk-price-input");
  const $bulkPriceApply = document.getElementById("bulk-price-apply");
  const $bulkPriceChips = document.getElementById("bulk-price-chips");

  // ---- Boot -------------------------------------------------------- //
  document.addEventListener("DOMContentLoaded", () => {
    if ($frameToggle) $frameToggle.addEventListener("change", onToggleChange);
    if ($frameSegment) {
      $frameSegment.addEventListener("click", (e) => {
        const btn = e.target.closest(".segment");
        if (!btn) return;
        state.frameMode = btn.dataset.value === "frame";
        $frameSegment.querySelectorAll(".segment").forEach(s => s.classList.remove("active"));
        btn.classList.add("active");
        renderGrid();
        if (state.selectedLayout) {
          const tile = state.catalog.tiles.find(t => t.layout === state.selectedLayout);
          if (tile) {
            const product = pickProduct(tile);
            if (product) selectProduct(tile, product);
          }
        }
      });
    }
    $mockupToggle.addEventListener("change", onMockupModeChange);
    $scanBtn.addEventListener("click", onScanClick);
    $renderAllBtn.addEventListener("click", onRenderAllClick);
    $downloadAllBtn.addEventListener("click", onDownloadAllClick);
    $listAllBtn.addEventListener("click", onListAllClick);
    if ($listAllScheduledBtn) {
      $listAllScheduledBtn.addEventListener("click", onListAllScheduledClick);
    }
    $backBtn.addEventListener("click", onBackClick);
    $bulkPriceApply.addEventListener("click", onBulkPriceApply);
    $bulkPriceInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") onBulkPriceApply();
    });

    // Lightbox: click outside the image, the close button, or press
    // Esc to dismiss. Clicking the image itself does NOT close so you
    // can still inspect detail.
    $lightbox.addEventListener("click", (e) => {
      if (e.target === $lightbox || e.target === $lightboxClose) closeLightbox();
    });
    $lightboxClose.addEventListener("click", closeLightbox);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$lightbox.classList.contains("hidden")) closeLightbox();
    });

    Promise.all([fetchConfig(), fetchCatalog()])
      .then(() => renderGrid())
      .catch(err => {
        console.error("Dashboard boot failed:", err);
        $grid.innerHTML = `<div class="loading">Failed to load: ${escapeHtml(err.message)}</div>`;
      });
  });

  // ---- Fetchers ---------------------------------------------------- //
  async function fetchCatalog() {
    const res = await fetch("/api/products");
    if (!res.ok) throw new Error(`GET /api/products → ${res.status}`);
    state.catalog = await res.json();
    $productCount.textContent = `${state.catalog.total_products} products`;
    $tileCount.textContent    = state.catalog.total_tiles;
  }

  async function fetchConfig() {
    try {
      const res = await fetch("/api/config");
      const cfg = await res.json();
      if (!cfg.ok) {
        showConfigBanner(cfg);
      }
    } catch (err) {
      console.warn("Config fetch failed:", err);
    }
  }

  // ---- Grid rendering --------------------------------------------- //
  function renderGrid() {
    if (!state.catalog) return;

    const groups = state.catalog.tile_groups || [];
    // Fallback: if backend hasn't been updated yet, use flat tiles list.
    if (!groups.length) {
      const tiles = state.catalog.tiles || [];
      if (!tiles.length) {
        $grid.innerHTML = `<div class="loading">No products in dashboard_order.</div>`;
        return;
      }
      $grid.innerHTML = "";
      for (const tile of tiles) $grid.appendChild(renderTile(tile));
      return;
    }

    $grid.innerHTML = "";
    for (const group of groups) {
      if (group.label) {
        const header = document.createElement("h3");
        header.className = "grid-group-label";
        header.textContent = group.label;
        $grid.appendChild(header);
      }
      const row = document.createElement("div");
      row.className = "grid-group";
      for (const tile of group.tiles) {
        row.appendChild(renderTile(tile));
      }
      $grid.appendChild(row);
    }
  }

  function renderTile(tile) {
    const product = pickProduct(tile);
    if (!product) {
      const el = document.createElement("div");
      el.className = "tile disabled";
      el.textContent = tile.layout;
      return el;
    }

    const el = document.createElement("div");
    el.className = "tile";
    if (!tile.has_toggle) el.classList.add("no-toggle");
    if (state.selectedLayout === tile.layout) el.classList.add("selected");
    el.dataset.layout     = tile.layout;
    el.dataset.productKey = product.product_key;

    el.innerHTML = `
      ${thumbMarkup(product)}
      <div class="tile-label">${escapeHtml(product.button_label)}</div>
    `;

    el.addEventListener("click", () => onTileClick(tile, product));
    return el;
  }

  function thumbMarkup(product) {
    if (product.preview_url) {
      const url = escapeHtml(product.preview_url);
      const alt = escapeHtml(product.button_label);
      return `
        <div class="tile-thumb">
          <img src="${url}" alt="${alt}"
               onerror="this.parentNode.classList.add('placeholder'); this.remove();" />
        </div>
      `;
    }
    return `
      <div class="tile-thumb placeholder">
        <span>${escapeHtml(placeholderLabel(product))}</span>
      </div>
    `;
  }

  function placeholderLabel(product) {
    if (product.layout && product.layout.startsWith("photo_")) return "photo";
    if (product.layout === "odd_card") return "card";
    if (product.layout === "odd_photo") return "photo";
    return "no preview";
  }

  function metaChips(product, tile) {
    const chips = [];
    if (product.main_size) chips.push(product.main_size);
    if (product.needs_secondary) chips.push(`+ ${product.needs_secondary}`);
    if (product.orientation_lock) chips.push(product.orientation_lock);
    if (tile.has_toggle) chips.push(state.frameMode ? "frame" : "mount");
    return chips.map(c => `<span class="chip">${escapeHtml(c)}</span>`).join("");
  }

  function pickProduct(tile) {
    if (state.frameMode && tile.frame) return tile.frame;
    if (tile.mount) return tile.mount;
    return tile.frame || null;
  }

  function sortedSuggestedPrices(product) {
    const list = (product?.suggested_prices || []).slice();
    list.sort((a, b) => Number(a) - Number(b));
    return list;
  }

  // ---- Grid events ------------------------------------------------- //
  function onMockupModeChange(e) {
    state.mockupOnly = !!e.target.checked;
    document.body.classList.toggle("mockup-mode", state.mockupOnly);
    $workflowHeading.textContent = state.mockupOnly
      ? "2 · Scan & render mockups"
      : "2 · Scan & list";
    // Re-render rows so the action buttons update (hide verify/list in mockup mode).
    if (state.rows.length) renderRows();
  }

  function onToggleChange(e) {
    state.frameMode = !!e.target.checked;
    renderGrid();
    if (state.selectedLayout) {
      const tile = state.catalog.tiles.find(t => t.layout === state.selectedLayout);
      if (tile) {
        const product = pickProduct(tile);
        if (product) selectProduct(tile, product);
      }
    }
  }

  function onTileClick(tile, product) {
    selectProduct(tile, product);
  }

  function selectProduct(tile, product) {
    state.selectedLayout = tile.layout;
    state.selectedProduct = product;

    // Reset workflow state — picking a new product clears any stale rows.
    state.rows = [];
    $rowsTable.classList.add("hidden");
    $rowsBody.innerHTML = "";
    $unmatched.classList.add("hidden");
    $unmatchedBody.innerHTML = "";
    $renderAllBtn.disabled = true;
    $listAllBtn.disabled = true;
    $scanStatus.textContent = "Not scanned yet.";
    $scanStatus.className = "scan-status muted";

    // Show workflow panel + populate header.
    $workflow.classList.remove("hidden");
    $productLabel.textContent  = product.button_label;
    $productKeyEl.textContent  = product.product_key;
    $defaultPrice.textContent  = `£${product.default_price_gbp.toFixed(2)}`;
    renderBulkPriceChips();
    $bulkPriceInput.value = "";

    // Re-render grid to show new .selected highlight.
    renderGrid();

    // Scroll the workflow into view.
    $workflow.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function onBackClick() {
    // Show the catalog again so Nicky can pick a different product.
    $catalog.classList.remove("hidden");
    $backBtn.classList.add("hidden");
    $catalog.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- Workflow: scan --------------------------------------------- //
  async function onScanClick() {
    if (!state.selectedProduct) return;
    setBusy(true);
    setScanStatus("Scanning ONE / TWO…", "info");
    try {
      const res  = await fetch("/api/match");
      const data = await res.json();
      if (!res.ok || !data) {
        throw new Error(data?.error || `GET /api/match → ${res.status}`);
      }
      await handleMatchReport(data);
    } catch (err) {
      setScanStatus(`Scan failed: ${err.message}`, "error");
      console.error(err);
    } finally {
      setBusy(false);
    }
  }

  async function handleMatchReport(report) {
    const matched = report.matched || [];
    const totals  = report.totals || {};

    // Default price for a new row = cheapest of the product's
    // `suggested_prices` (the bottom chip). The filename-parsed price
    // (m.picture.price) still wins if it's present, but in practice
    // Kim's photos don't embed prices, so that branch is rarely hit.
    const sortedPrices = sortedSuggestedPrices(state.selectedProduct);
    const floorPrice = sortedPrices.length
      ? Number(sortedPrices[0])
      : state.selectedProduct.default_price_gbp;

    // Products with no `needs_secondary` (photo-only + odd-size card/
    // photo) don't require a matching file in TWO/ — pull those in
    // from unmatched_pictures so Nicky doesn't have to fake cards to
    // get rows.
    const needsSecondary = !!state.selectedProduct.needs_secondary;
    const extra = needsSecondary
      ? []
      : (report.unmatched_pictures || [])
          .filter((p) => p.pair_key && p.is_jpg)
          .map((p) => ({
            pair_key: p.pair_key,
            parsed:   p.parsed,
            picture:  p,
            card:     null,
          }));

    state.rows = [...matched, ...extra].map((m) => ({
      pair_key:     m.pair_key,
      parsed:       m.parsed,
      picture:      m.picture,
      card:         m.card,
      mockup_url:   null,
      mockup_path:  null,
      is_raw_photo: false,
      price:        m.picture?.price ?? floorPrice,
      title:        "",       // filled in by /api/preview
      title_dirty:  false,    // true once the user edits the title
      status:       "idle",
      message:      "",
    }));

    // Summary line
    const parts = [];
    parts.push(`${totals.matched ?? matched.length} matched`);
    if ((totals.unmatched_pictures ?? 0) > 0) parts.push(`${totals.unmatched_pictures} unmatched picture(s)`);
    if ((totals.unmatched_cards ?? 0)    > 0) parts.push(`${totals.unmatched_cards} unmatched card(s)`);
    if ((totals.needs_normalize ?? 0)    > 0) parts.push(`${totals.needs_normalize} need normalize`);
    if ((totals.unknown_format ?? 0)     > 0) parts.push(`${totals.unknown_format} unknown format`);
    setScanStatus(parts.join(" · ") || "No files found.",
                  report.ok ? "ok" : "warn");

    renderRows();
    renderBulkPriceChips();
    renderUnmatched(report);
    $renderAllBtn.disabled  = state.rows.length === 0;
    $downloadAllBtn.disabled = state.rows.length === 0;
    $listAllBtn.disabled    = state.rows.length === 0;
    if ($listAllScheduledBtn) $listAllScheduledBtn.disabled = state.rows.length === 0;

    // Hide catalog + show back-button once we have rows to work with.
    if (state.rows.length > 0) {
      $catalog.classList.add("hidden");
      $backBtn.classList.remove("hidden");
    }

    // Kick off title previews in parallel (pure endpoint, no eBay).
    // Skip in mockup-only mode — no listing titles needed.
    if (!state.mockupOnly) {
      await Promise.all(state.rows.map((_, i) => previewRow(i)));
    }
  }

  // ---- Workflow: preview (pure, no eBay) -------------------------- //
  async function previewRow(i) {
    const row = state.rows[i];
    if (!row || !state.selectedProduct) return;
    try {
      const res = await fetch("/api/preview", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          product_key: state.selectedProduct.product_key,
          pair_key:    row.pair_key,
          price_gbp:   row.price,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `POST /api/preview → ${res.status}`);
      if (!row.title_dirty) {
        row.title = data?.summary?.title || "";
      }
      // Rebuild just this row so the title input populates.
      const tr = $rowsBody.querySelector(`tr[data-idx="${i}"]`);
      if (tr) tr.replaceWith(renderRow(i));
    } catch (err) {
      console.warn("preview failed for row", i, err);
    }
  }

  // ---- Workflow: row rendering ------------------------------------ //
  function renderRows() {
    $rowsBody.innerHTML = "";
    if (!state.rows.length) {
      $rowsTable.classList.add("hidden");
      return;
    }
    $rowsTable.classList.remove("hidden");
    for (let i = 0; i < state.rows.length; i++) {
      $rowsBody.appendChild(renderRow(i));
    }
  }

  function renderRow(i) {
    const row = state.rows[i];
    const tr  = document.createElement("tr");
    tr.dataset.idx = String(i);
    tr.className = `row-${row.status}`;

    // --- Thumb cell --------------------------------------------------- //
    const tdThumb = document.createElement("td");
    tdThumb.className = "col-thumb";
    if (row.mockup_url) {
      const img = document.createElement("img");
      img.className = "row-thumb";
      img.src = row.mockup_url;
      img.alt = "mockup preview — click to enlarge";
      img.title = "Click to enlarge";
      img.addEventListener("click", () => openLightbox(row.mockup_url));
      tdThumb.appendChild(img);
    } else {
      tdThumb.innerHTML = `<div class="row-thumb placeholder">—</div>`;
    }
    tr.appendChild(tdThumb);

    // --- Pair / title cell ------------------------------------------- //
    const tdPair = document.createElement("td");
    tdPair.className = "col-pair";
    const parsed = row.parsed || {};

    const nameDiv = document.createElement("div");
    nameDiv.className = "pair-name";
    nameDiv.textContent = parsed.name || row.pair_key;
    tdPair.appendChild(nameDiv);

    const metaDiv = document.createElement("div");
    metaDiv.className = "pair-meta";
    metaDiv.innerHTML = `
      ${parsed.field1  ? `<span class="chip">${escapeHtml(parsed.field1)}</span>`  : ""}
      ${parsed.category? `<span class="chip">${escapeHtml(parsed.category)}</span>`: ""}
      ${parsed.variant ? `<span class="chip">${escapeHtml(parsed.variant)}</span>` : ""}
    `;
    tdPair.appendChild(metaDiv);

    // Editable title input — the full eBay title, capped at 80 chars by
    // the Trading API. Counter shows live length so Nicky knows when
    // she's blown the budget.
    const titleWrap = document.createElement("div");
    titleWrap.className = "title-edit";
    const titleLabel = document.createElement("label");
    titleLabel.className = "title-label";
    titleLabel.textContent = "Title";
    const titleInput = document.createElement("input");
    titleInput.type  = "text";
    titleInput.className = "title-input";
    titleInput.maxLength = 80;
    titleInput.placeholder = row.title ? "" : "Loading preview…";
    titleInput.value = row.title || "";
    const titleCount = document.createElement("span");
    titleCount.className = "title-count";
    titleCount.textContent = `${(row.title || "").length} / 80`;
    titleInput.addEventListener("input", (e) => {
      row.title = e.target.value;
      row.title_dirty = true;
      titleCount.textContent = `${row.title.length} / 80`;
      titleCount.classList.toggle("over", row.title.length > 80);
    });
    titleWrap.appendChild(titleLabel);
    titleWrap.appendChild(titleInput);
    titleWrap.appendChild(titleCount);
    tdPair.appendChild(titleWrap);

    tr.appendChild(tdPair);

    // --- Price cell --------------------------------------------------- //
    const tdPrice = document.createElement("td");
    tdPrice.className = "col-price";

    // A flex container so the price input and the suggestion chips sit
    // side-by-side on a single horizontal row.
    const priceCell = document.createElement("div");
    priceCell.className = "price-cell";

    const priceInput = document.createElement("input");
    priceInput.type  = "number";
    priceInput.min   = "0";
    priceInput.step  = "1";
    priceInput.value = row.price;
    priceInput.addEventListener("input", (e) => {
      row.price = parseFloat(e.target.value) || 0;
      // Refresh the /api/preview so the title reflects the new price
      // (best-offer thresholds depend on it) — but only if the user
      // hasn't manually edited the title.
      if (!row.title_dirty) previewRow(i);
      updateChipHighlights(wrap, row.price);
    });
    priceInput.addEventListener("blur", (e) => {
      const v = parseFloat(e.target.value);
      if (!isFinite(v) || v <= 0) return;
      const frac = Math.round((v - Math.floor(v)) * 100);
      if (frac === 99) return;
      const snapped = Math.max(0, Math.floor(v) - 0 + 0.99);
      e.target.value = snapped.toFixed(2);
      row.price = snapped;
      updateChipHighlights(wrap, row.price);
      if (!row.title_dirty) previewRow(i);
    });
    priceCell.appendChild(priceInput);

    // Quick-select chips — sorted ascending (cheapest first), left to right.
    const suggested = sortedSuggestedPrices(state.selectedProduct);
    const wrap = document.createElement("div");
    wrap.className = "price-suggestions";
    for (const p of suggested) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "price-chip";
      chip.dataset.price = String(p);
      if (Math.abs(p - row.price) < 0.005) chip.classList.add("active");
      chip.textContent = `£${Number(p).toFixed(2)}`;
      chip.addEventListener("click", () => {
        row.price = Number(p);
        priceInput.value = row.price;
        updateChipHighlights(wrap, row.price);
        if (!row.title_dirty) previewRow(i);
      });
      wrap.appendChild(chip);
    }
    priceCell.appendChild(wrap);
    tdPrice.appendChild(priceCell);

    tr.appendChild(tdPrice);

    // --- Status cell -------------------------------------------------- //
    const tdStatus = document.createElement("td");
    tdStatus.className = `col-status status-${row.status}`;
    tdStatus.textContent = statusLabel(row);
    tr.appendChild(tdStatus);

    // --- Actions cell ------------------------------------------------- //
    const isTemplated = !!state.selectedProduct?.template_id;
    const isRaw       = !isTemplated || row.is_raw_photo;
    const readyToList = isRaw || !!row.mockup_url;

    const tdActions = document.createElement("td");
    tdActions.className = "col-actions";
    const actionsWrap = document.createElement("div");
    actionsWrap.className = "actions-wrap";

    if (isTemplated) {
      const btnRender = document.createElement("button");
      btnRender.className = "btn btn-small";
      btnRender.textContent = row.mockup_url ? "Re-render" : "Render";
      btnRender.addEventListener("click", () => renderRowMockup(i));
      actionsWrap.appendChild(btnRender);
    }

    if (row.mockup_url) {
      const btnView = document.createElement("button");
      btnView.className = "btn btn-small";
      btnView.textContent = "View";
      btnView.title = "Open full-size mockup";
      btnView.addEventListener("click", () => openLightbox(row.mockup_url));
      actionsWrap.appendChild(btnView);

      // In mockup-only mode, offer a download link instead of verify/list.
      if (state.mockupOnly) {
        const btnDownload = document.createElement("a");
        btnDownload.className = "btn btn-small btn-primary";
        btnDownload.textContent = "Download";
        btnDownload.href = row.mockup_url;
        btnDownload.download = mockupFilename(row);
        btnDownload.title = "Save mockup image";
        actionsWrap.appendChild(btnDownload);
      }
    }

    if (!state.mockupOnly) {
      const btnVerify = document.createElement("button");
      btnVerify.className = "btn btn-small";
      btnVerify.textContent = "Verify";
      btnVerify.disabled = !readyToList;
      btnVerify.title = readyToList ? "" : "Render the mockup first";
      btnVerify.addEventListener("click", () => verifyRow(i));
      actionsWrap.appendChild(btnVerify);

      const btnListScheduled = document.createElement("button");
      btnListScheduled.className = "btn btn-small";
      btnListScheduled.textContent = "List scheduled";
      btnListScheduled.disabled = !readyToList;
      btnListScheduled.title = readyToList
        ? "Schedule this listing for the date/time above"
        : "Render the mockup first";
      btnListScheduled.addEventListener("click", () => listRow(i, false, true));
      actionsWrap.appendChild(btnListScheduled);

      const btnList = document.createElement("button");
      btnList.className = "btn btn-small btn-danger";
      btnList.textContent = "List live";
      btnList.disabled = !readyToList;
      btnList.title = readyToList ? "" : "Render the mockup first";
      btnList.addEventListener("click", () => listRow(i));
      actionsWrap.appendChild(btnList);
    }

    tdActions.appendChild(actionsWrap);
    tr.appendChild(tdActions);

    return tr;
  }

  function updateChipHighlights(wrap, price) {
    wrap.querySelectorAll(".price-chip").forEach(c => {
      const p = parseFloat(c.dataset.price);
      c.classList.toggle("active", Math.abs(p - price) < 0.005);
    });
  }

  function statusLabel(row) {
    if (row.message) return row.message;
    switch (row.status) {
      case "idle":      return "—";
      case "rendering": return "Rendering…";
      case "rendered":  return "Mockup ready";
      case "verifying": return "Verifying…";
      case "verified":  return "Verified OK";
      case "listing":   return "Listing…";
      case "listed":    return "Listed";
      case "error":     return "Error";
      default:          return row.status;
    }
  }

  function renderUnmatched(report) {
    const buckets = [
      { label: "Unmatched pictures", list: report.unmatched_pictures },
      { label: "Unmatched cards",    list: report.unmatched_cards },
      { label: "Needs normalize",    list: report.needs_normalize },
      { label: "Unknown format",     list: report.unknown_format },
    ].filter(b => (b.list || []).length > 0);

    if (!buckets.length) {
      $unmatched.classList.add("hidden");
      $unmatchedBody.innerHTML = "";
      return;
    }

    $unmatched.classList.remove("hidden");
    $unmatchedBody.innerHTML = buckets.map(b => `
      <div class="bucket">
        <h4>${escapeHtml(b.label)} (${b.list.length})</h4>
        <ul>${b.list.map(f => `<li>${escapeHtml(f.name)}</li>`).join("")}</ul>
      </div>
    `).join("");
  }

  // ---- Workflow: per-row actions ---------------------------------- //
  function listPayload(row, overrides = {}) {
    return {
      product_key:    state.selectedProduct.product_key,
      pair_key:       row.pair_key,
      price_gbp:      row.price,
      title_override: row.title_dirty ? row.title : null,
      ...overrides,
    };
  }

  async function renderRowMockup(i) {
    const row = state.rows[i];
    if (!row || !state.selectedProduct) return;
    setRowStatus(i, "rendering", "");
    try {
      const res = await fetch("/api/mockup", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          product_key: state.selectedProduct.product_key,
          pair_key:    row.pair_key,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `POST /api/mockup → ${res.status}`);
      row.mockup_url   = data.mockup_url || data.mockup_path || null;
      row.mockup_path  = data.mockup_path;
      row.is_raw_photo = !!data.is_raw_photo;
      if (row.is_raw_photo) {
        setRowStatus(i, "rendered", "Photo-only (no mockup)");
      } else {
        setRowStatus(i, "rendered", "");
      }
    } catch (err) {
      setRowStatus(i, "error", err.message);
    }
  }

  async function onRenderAllClick() {
    for (let i = 0; i < state.rows.length; i++) {
      if (state.rows[i].mockup_url || state.rows[i].is_raw_photo) continue;
      await renderRowMockup(i);
    }
  }

  async function onDownloadAllClick() {
    const ready = state.rows.filter(r => r.mockup_url);
    if (!ready.length) {
      alert("No mockups rendered yet — hit 'Render all mockups' first.");
      return;
    }
    if (!state.selectedProduct) return;

    // Ask the server to zip all rendered mockups into one archive.
    try {
      $downloadAllBtn.disabled = true;
      $downloadAllBtn.textContent = "Zipping…";
      const res = await fetch("/api/download-mockups", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          product_key: state.selectedProduct.product_key,
          pair_keys:   ready.map(r => r.pair_key),
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.detail || `POST /api/download-mockups → ${res.status}`);
      }
      // Stream the zip blob and trigger a browser download.
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `${state.selectedProduct.product_key}_mockups.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Download failed: ${err.message}`);
    } finally {
      $downloadAllBtn.disabled = false;
      $downloadAllBtn.textContent = "Download all";
    }
  }

  async function verifyRow(i) {
    const row = state.rows[i];
    if (!row || !state.selectedProduct) return;
    setRowStatus(i, "verifying", "");
    try {
      const res = await fetch("/api/list", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(listPayload(row, {
          verify_only: true,
          confirm:     false,
        })),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `POST /api/list → ${res.status}`);
      // Sync the title back from the actual built listing, unless the
      // user has explicitly edited it (respect their override).
      const builtTitle = data?.summary?.title;
      if (builtTitle && !row.title_dirty) row.title = builtTitle;
      setRowStatus(i, "verified",
                   builtTitle ? `OK · ${truncate(builtTitle, 60)}` : "Verified OK");
    } catch (err) {
      setRowStatus(i, "error", err.message);
    }
  }

  function scheduleAtIso() {
    if (!$scheduleInput || !$scheduleInput.value) return null;
    // datetime-local gives "YYYY-MM-DDTHH:MM" (no timezone). Treat as
    // local time and convert to ISO8601 so the server parses it as
    // a real instant.
    const dt = new Date($scheduleInput.value);
    if (isNaN(dt.getTime())) return null;
    return dt.toISOString();
  }

  async function listRow(i, skipConfirm = false, scheduled = false) {
    const row = state.rows[i];
    if (!row || !state.selectedProduct) return;

    let scheduleAt = null;
    if (scheduled) {
      scheduleAt = scheduleAtIso();
      if (!scheduleAt) {
        alert("Pick a schedule date/time in the 'Schedule for' box first.");
        return;
      }
    }

    if (!skipConfirm) {
      const title = row.title || row.parsed?.name || row.pair_key;
      const prompt = scheduled
        ? `Schedule listing for "${title}" at £${row.price.toFixed(2)} for ${$scheduleInput.value.replace("T", " ")}?`
        : `Submit LIVE listing for "${title}" at £${row.price.toFixed(2)}? This cannot be undone.`;
      if (!confirm(prompt)) return;
    }

    setRowStatus(i, scheduled ? "scheduling" : "listing", "");
    try {
      const res = await fetch("/api/list", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(listPayload(row, {
          verify_only: false,
          confirm:     true,
          schedule_at: scheduleAt,
        })),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `POST /api/list → ${res.status}`);
      const itemId = data?.result?.ItemID || data?.result?.item_id || "";
      const label  = scheduled ? "Scheduled" : "Listed";
      setRowStatus(i, "listed", itemId ? `${label} · ${itemId}` : label);
    } catch (err) {
      setRowStatus(i, "error", err.message);
    }
  }

  function renderBulkPriceChips() {
    if (!$bulkPriceChips) return;
    $bulkPriceChips.innerHTML = "";
    if (!state.selectedProduct) return;
    const suggested = sortedSuggestedPrices(state.selectedProduct);
    for (const p of suggested) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "price-chip";
      chip.dataset.price = String(p);
      chip.textContent = `£${Number(p).toFixed(2)}`;
      chip.addEventListener("click", () => {
        applyBulkPrice(Number(p));
      });
      $bulkPriceChips.appendChild(chip);
    }
  }

  function applyBulkPrice(rawPrice) {
    if (!state.rows.length) return;
    if (!isFinite(rawPrice) || rawPrice <= 0) return;
    // Snap to .99 for consistency with the per-row blur handler.
    let price = rawPrice;
    const frac = Math.round((price - Math.floor(price)) * 100);
    if (frac !== 99) price = Math.max(0, Math.floor(price) + 0.99);
    price = Number(price.toFixed(2));

    for (let i = 0; i < state.rows.length; i++) {
      // Don't clobber already-listed rows.
      if (state.rows[i].status === "listed") continue;
      state.rows[i].price = price;
    }
    renderRows();
    // Re-run previews for any row whose title isn't dirty so best-offer
    // thresholds refresh off the new price.
    state.rows.forEach((r, i) => { if (!r.title_dirty) previewRow(i); });
    $bulkPriceInput.value = price.toFixed(2);
  }

  function onBulkPriceApply() {
    const raw = parseFloat($bulkPriceInput.value);
    if (!isFinite(raw) || raw <= 0) {
      $bulkPriceInput.focus();
      return;
    }
    applyBulkPrice(raw);
  }

  async function onListAllClick() {
    if (!state.rows.length) return;
    const eligible = state.rows
      .map((row, i) => ({ row, i }))
      .filter(({ row }) => {
        if (row.status === "listed") return false;
        const isTemplated = !!state.selectedProduct?.template_id;
        const isRaw = !isTemplated || row.is_raw_photo;
        return isRaw || !!row.mockup_url;
      });

    if (!eligible.length) {
      alert("No rows are ready to list — render mockups first.");
      return;
    }

    const total = eligible.reduce((acc, { row }) => acc + (row.price || 0), 0);
    const msg =
      `Submit ${eligible.length} LIVE listing(s) for a total of £${total.toFixed(2)}?\n\n` +
      `This cannot be undone.`;
    if (!confirm(msg)) return;

    for (const { i } of eligible) {
      // eslint-disable-next-line no-await-in-loop
      await listRow(i, /* skipConfirm */ true);
    }
  }

  async function onListAllScheduledClick() {
    if (!state.rows.length) return;
    const scheduleAt = scheduleAtIso();
    if (!scheduleAt) {
      alert("Pick a schedule date/time in the 'Schedule for' box first.");
      return;
    }
    const eligible = state.rows
      .map((row, i) => ({ row, i }))
      .filter(({ row }) => {
        if (row.status === "listed") return false;
        const isTemplated = !!state.selectedProduct?.template_id;
        const isRaw = !isTemplated || row.is_raw_photo;
        return isRaw || !!row.mockup_url;
      });

    if (!eligible.length) {
      alert("No rows are ready to list — render mockups first.");
      return;
    }

    const total = eligible.reduce((acc, { row }) => acc + (row.price || 0), 0);
    const when = $scheduleInput.value.replace("T", " ");
    const msg =
      `Schedule ${eligible.length} listing(s) for ${when}, total £${total.toFixed(2)}?`;
    if (!confirm(msg)) return;

    for (const { i } of eligible) {
      // eslint-disable-next-line no-await-in-loop
      await listRow(i, /* skipConfirm */ true, /* scheduled */ true);
    }
  }

  function setRowStatus(i, status, message) {
    const row = state.rows[i];
    if (!row) return;
    row.status  = status;
    row.message = message || "";
    const oldTr = $rowsBody.querySelector(`tr[data-idx="${i}"]`);
    if (oldTr) {
      const newTr = renderRow(i);
      oldTr.replaceWith(newTr);
    }
  }

  // ---- UI helpers ------------------------------------------------- //
  function setBusy(busy) {
    state.busy = busy;
    $scanBtn.disabled = busy;
  }

  function setScanStatus(text, kind) {
    $scanStatus.textContent = text;
    $scanStatus.className = `scan-status ${kind || "muted"}`;
  }

  function openLightbox(url) {
    if (!url) return;
    $lightboxImg.src = url;
    $lightbox.classList.remove("hidden");
  }

  function closeLightbox() {
    $lightbox.classList.add("hidden");
    $lightboxImg.src = "";
  }

  function showConfigBanner(cfg) {
    const missing = [];
    if (cfg.one && !cfg.one.exists) missing.push(`ONE (${cfg.one.path || "not set"})`);
    if (cfg.two && !cfg.two.exists) missing.push(`TWO (${cfg.two.path || "not set"})`);
    const msg = missing.length
      ? `Folder(s) missing: ${missing.join(" · ")}`
      : (cfg.error || "Config problem — check ~/.klh/config.yaml");
    $configBanner.textContent = msg;
    $configBanner.classList.remove("hidden");
  }

  // ---- Utils ------------------------------------------------------- //
  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function truncate(s, n) {
    s = String(s ?? "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function mockupFilename(row) {
    // Match the ONE folder filename convention: pair_key is already
    // in the right format (e.g. "Seamus_Coleman_Everton_Football").
    const stem = (row.pair_key || row.parsed?.name || "mockup")
      .replace(/\s+/g, "_");
    return stem + ".jpg";
  }
})();
