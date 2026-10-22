/* ──────────────────────────────────────────────────────────────────────────
   Stock audit: warning about fresh orders.

   When a manager sets a variant's stock level manually, part of the goods is still
   physically on the shelf but has already been sold and WRITTEN OFF from stock by fresh orders
   (new/processing). If the physical count is entered "as is", these units
   are counted twice. The modal shows how many have already been written off and offers to subtract
   them.

   Public API:
     window.StockDemand.check(variantId, typedValue)
       → Promise<{action, value, demand}>
         action: 'proceed' | 'keep' | 'subtract' | 'cancel'
         proceed/keep/subtract → save value; cancel → roll back.
   ──────────────────────────────────────────────────────────────────────────
 */
(function () {
  function fetchDemand(variantId) {
    return fetch('/admin/catalog/variant-fresh-demand/?id=' + encodeURIComponent(variantId),
      { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function showModal(typed, demand) {
    return new Promise(function (resolve) {
      var held = demand.held || 0;
      var result = typed - held;

      var overlay = document.createElement('div');
      overlay.className = 'sdm-overlay';
      overlay.innerHTML =
        '<div class="sdm-box" role="dialog" aria-modal="true" aria-label="Fresh orders">' +
          '<div class="sdm-head">Product in fresh orders</div>' +
          '<div class="sdm-body">' +
            '<div class="sdm-fact">In orders: <b>' + held + ' pcs</b></div>' +
          '</div>' +
          '<div class="sdm-actions">' +
            '<button type="button" class="sdm-btn sdm-cancel">Cancel</button>' +
            '<button type="button" class="sdm-btn sdm-keep">Keep ' + typed + '</button>' +
            '<button type="button" class="sdm-btn sdm-primary sdm-subtract">Set ' + result + '</button>' +
          '</div>' +
        '</div>';

      function close(action, value) {
        document.removeEventListener('keydown', onKey, true);
        overlay.remove();
        resolve({ action: action, value: value });
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close('cancel'); }
        else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close('subtract', result); }
      }
      overlay.querySelector('.sdm-cancel').addEventListener('click', function () { close('cancel'); });
      overlay.querySelector('.sdm-keep').addEventListener('click', function () { close('keep', typed); });
      overlay.querySelector('.sdm-subtract').addEventListener('click', function () { close('subtract', result); });
      overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close('cancel'); });
      document.addEventListener('keydown', onKey, true);

      document.body.appendChild(overlay);
      var primary = overlay.querySelector('.sdm-subtract');
      if (primary) primary.focus();
    });
  }

  function check(variantId, typedValue) {
    var typed = parseInt(typedValue, 10) || 0;
    return fetchDemand(variantId).then(function (demand) {
      if (!demand || !demand.ok || !(demand.held > 0)) {
        return { action: 'proceed', value: typed, demand: demand };
      }
      return showModal(typed, demand).then(function (res) {
        return { action: res.action, value: (res.value != null ? res.value : typed), demand: demand };
      });
    });
  }

  window.StockDemand = { check: check, fetch: fetchDemand };
})();
