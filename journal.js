async function deleteTrade(tradeId, bucket) {
  if (!confirm('Delete this trade? This cannot be undone.')) return;
  try {
    var r = await fetch('/journal-delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({trade_id: tradeId, bucket: bucket === 'open' ? 'trades' : 'closed'})
    });
    var d = await r.json();
    if (d.success) { location.reload(); }
    else { alert('Error: ' + d.error); }
  } catch(e) { alert('Delete failed: ' + e.message); }
}

function makeEditable(td, tradeId, field) {
  if (td.querySelector('input')) return;
  var orig = td.innerText.trim();
  var wrapper = document.createElement('div');
  wrapper.style.cssText = 'display:flex;gap:4px;align-items:center;flex-wrap:nowrap';
  var inp = document.createElement('input');
  inp.style.cssText = 'border:1.5px solid #6366f1;border-radius:4px;padding:3px 6px;font-size:12px;background:#0f172a;color:#f1f5f9;flex:1;min-width:50px;max-width:150px';
  inp.value = orig;
  var btn = document.createElement('button');
  btn.textContent = 'Save';
  btn.style.cssText = 'background:#22c55e;color:white;border:none;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px;font-weight:bold;white-space:nowrap';
  var cancel = document.createElement('span');
  cancel.textContent = 'x';
  cancel.style.cssText = 'color:#94a3b8;cursor:pointer;font-size:16px;padding:0 4px';
  wrapper.appendChild(inp);
  wrapper.appendChild(btn);
  wrapper.appendChild(cancel);
  td.innerHTML = '';
  td.appendChild(wrapper);
  inp.focus(); inp.select();

  function save() {
    var val = inp.value.trim();
    if (val === orig) { td.innerText = orig; attachEditors(); return; }
    btn.disabled = true; btn.textContent = '...';
    fetch('/journal-edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({trade_id: tradeId, field: field, value: val})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.success) {
        td.innerText = val;
        td.style.background = 'rgba(34,197,94,0.2)';
        // If P&L was recalculated, update the P&L cell in same row
        if (data.pnl_str) {
          var row = td.closest('tr');
          if (row) {
            var pnlCell = row.querySelector('[data-edit="pnl_total"]');
            if (pnlCell) {
              pnlCell.innerText = data.pnl_str;
              pnlCell.style.background = 'rgba(34,197,94,0.2)';
              var col = data.pnl_total >= 0 ? '#22c55e' : '#ef4444';
              pnlCell.style.color = col;
              setTimeout(function() { pnlCell.style.background = ''; }, 1500);
            }
          }
        }
        setTimeout(function() { td.style.background = ''; attachEditors(); }, 1500);
      } else {
        td.innerText = orig; attachEditors();
        alert('Save error: ' + (data.error || 'Unknown error'));
      }
    })
    .catch(function(err) { td.innerText = orig; attachEditors(); alert('Save failed: ' + err.message); });
  }

  btn.addEventListener('click', save);
  cancel.addEventListener('click', function() { td.innerText = orig; attachEditors(); });
  inp.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') { td.innerText = orig; attachEditors(); }
  });
}

function attachEditors() {
  document.querySelectorAll('[data-edit]').forEach(function(td) {
    if (td._hasEditor) return;
    td._hasEditor = true;
    td.addEventListener('click', function() {
      makeEditable(td, td.dataset.tradeId, td.dataset.edit);
    });
  });
}
document.addEventListener('DOMContentLoaded', attachEditors);
document.querySelectorAll('a').forEach(function(a) {
  a.addEventListener('click', function() { setTimeout(attachEditors, 300); });
});
