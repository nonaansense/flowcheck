async function closeTrade(id){
var o=document.createElement('div');
o.style='position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center';
var inner=document.createElement('div');
inner.style='background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px;width:290px;max-width:92vw';
var h3=document.createElement('h3');h3.style='color:#f1f5f9;margin:0 0 14px';h3.textContent='Close Position';inner.appendChild(h3);
var l1=document.createElement('div');l1.style='color:#94a3b8;font-size:13px;margin-bottom:4px';l1.textContent='Exit Price';inner.appendChild(l1);
var ep=document.createElement('input');ep.id='ep';ep.type='number';ep.step='0.01';ep.placeholder='e.g. 2.40';ep.style='width:100%;padding:8px;background:#0f172a;border:1px solid #475569;border-radius:6px;color:#f1f5f9;font-size:15px;box-sizing:border-box;margin-bottom:12px';inner.appendChild(ep);
var l2=document.createElement('div');l2.style='color:#94a3b8;font-size:13px;margin-bottom:4px';l2.textContent='Contracts (blank=all)';inner.appendChild(l2);
var ec=document.createElement('input');ec.id='ec';ec.type='number';ec.step='1';ec.placeholder='blank = full exit';ec.style='width:100%;padding:8px;background:#0f172a;border:1px solid #475569;border-radius:6px;color:#f1f5f9;font-size:15px;box-sizing:border-box;margin-bottom:16px';inner.appendChild(ec);
var row=document.createElement('div');row.style='display:flex;gap:8px';
var bok=document.createElement('button');bok.id='bok';bok.style='flex:1;padding:10px;background:#22c55e;color:#fff;border:none;border-radius:6px;font-size:15px;font-weight:700;cursor:pointer';bok.textContent='Confirm';row.appendChild(bok);
var bno=document.createElement('button');bno.id='bno';bno.style='flex:1;padding:10px;background:#475569;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer';bno.textContent='Cancel';row.appendChild(bno);
inner.appendChild(row);o.appendChild(inner);document.body.appendChild(o);
ep.focus();
bno.onclick=function(){document.body.removeChild(o)};
o.onclick=function(e){if(e.target===o)document.body.removeChild(o)};
bok.onclick=async function(){
var p=parseFloat(ep.value);
var c=parseInt(ec.value)||null;
if(!p||p<=0){ep.style.borderColor='#ef4444';return;}
var n=new Date(),pad=function(x){return x.toString().padStart(2,'0')};
var hr=n.getHours(),ap=hr>=12?'PM':'AM';hr=hr%12||12;
var b={trade_id:id,exit_price:p,exit_date:n.getFullYear()+'-'+pad(n.getMonth()+1)+'-'+pad(n.getDate()),exit_time:hr+':'+pad(n.getMinutes())+ap};
if(c)b.contracts=c;
bok.textContent='...';bok.disabled=true;
try{
var r=await fetch('/journal-close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
var d=await r.json();document.body.removeChild(o);
if(d.success){alert(d.ticker+' closed\nP&L: '+d.pnl_str+' ('+d.pnl_pct+')');location.reload();}
else alert('Error: '+d.error);
}catch(e){document.body.removeChild(o);alert('Error: '+e.message);}
};
}
