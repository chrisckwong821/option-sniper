// Deliberately synthetic examples. Never used by app.js or the live feed.
const examples = [
  {ticker:'NVDA',name:'NVIDIA',gain:3.8,priceZ:1.6,values:[2,3.8,5.1,7.4,9.3,12],scores:[.3,.7,1,1.5,1.9,2.4],n:108,reading:'Expansion + unusual',note:'IV rose 12%, and the change sits well above its usual same-time pattern.'},
  {ticker:'TSLA',name:'Tesla',gain:4.2,priceZ:1.3,values:[5,9,12,15,17,18],scores:[.2,.3,.4,.5,.6,.6],n:105,reading:'Large move, familiar pattern',note:'IV rose more than NVIDIA’s, but this magnitude is closer to Tesla’s own historical pattern.'},
  {ticker:'MSFT',name:'Microsoft',gain:1.8,priceZ:1.2,values:[.4,1,1.5,2.4,3.6,5],scores:[.2,.6,1,1.7,2.5,3.1],n:111,reading:'Smaller move, bigger surprise',note:'A 5% rise is modest in raw terms, but unusually large relative to this stock’s same-time history.'},
  {ticker:'AMD',name:'Advanced Micro Devices',gain:2.9,priceZ:1.4,values:[1,.4,-1,-2,-3,-4],scores:[.1,-.1,-.5,-.8,-1.1,-1.4],n:99,reading:'IV contracting',note:'Price is up while IV has fallen. The negative sigma also places the IV change below its historical median.'},
  {ticker:'ARM',name:'Arm Holdings',gain:3.2,priceZ:1.1,values:[1,2,3,4,6,8],scores:[null,null,null,null,null,null],n:12,reading:'Magnitude only · short history',note:'The IV increase is observable, but history is insufficient to judge how unusual it is.'},
];
const times=['09:30','10:00','10:30','11:00','11:30','12:00'];
const signed=(v,suffix)=>`${v>=0?'+':''}${v.toFixed(1)}${suffix}`;
const badge=z=>`<span class="badge ${z===null||Math.abs(z)<1?'quiet':z<0?'negative':''}">${z===null?'Unscored':signed(z,'σ')}</span>`;
function render(){
 const sorted=[...examples].sort((a,b)=>document.querySelector('#sort').value==='sigma'?(b.scores[5]??-Infinity)-(a.scores[5]??-Infinity):b.values[5]-a.values[5]);
 document.querySelector('#examples').innerHTML=sorted.map(r=>`<tr><td><strong>${r.ticker}</strong><small>${r.name}</small></td><td>${signed(r.gain,'%')}</td><td>+${r.priceZ.toFixed(1)}</td><td><span class="pair-button term-tip" tabindex="0" data-tooltip="${r.note} Same ticker, 12:00 ET bucket, front-weekly tenor. ${r.n} illustrative historical observations."><strong>${signed(r.values[5],'%')}</strong>${badge(r.scores[5])}</span><small>12:00–12:30 ET · mark 12:29</small></td></tr>`).join('');
}
document.querySelector('#sort').addEventListener('change',render);
render();
