'use strict';
/* V4.2: replaces the browser-only URL/media import paths with a same-origin API.
   Recipe library and meal planning still persist in localStorage on the device. */
const V42_EXAMPLE = 'https://www.instagram.com/reel/DeBvYW5u80z/';
const v42Live = location.protocol === 'https:' || /^localhost$|^127\.0\.0\.1$/.test(location.hostname);

function v42ServerCall(endpoint, options={}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12 * 60 * 1000);
  return fetch(endpoint, {credentials:'same-origin',...options,signal:controller.signal})
    .then(async response => {
      if(response.ok) return response.json();
      let detail;
      try{detail=(await response.json()).detail;}catch{detail=`Erreur HTTP ${response.status}`;}
      const message=typeof detail==='object' ? detail.message || JSON.stringify(detail) : detail;
      throw Error(String(message || `Erreur HTTP ${response.status}`));
    }).finally(()=>clearTimeout(timer));
}
function v42ReviewResult(result, {sourceUrl='',label='Analyse serveur'}={}) {
  if(!result || !result.recipe) throw Error('Réponse du serveur invalide.');
  if(result.status==='none') return false;
  const r=result.recipe;
  const parsed={title:r.title||'Recette à vérifier',ingredients:Array.isArray(r.ingredients)?r.ingredients:[],
    steps:Array.isArray(r.steps)?r.steps:[],minutes:r.minutes||0,servings:r.servings||2,
    description:r.description||'Ingrédients et étapes générés à partir des sources disponibles : vérifiez le résultat.'};
  const warnings=Array.isArray(result.warnings)?result.warnings.filter(Boolean).slice(0,3).join(' · '):'';
  bv4Review(parsed,{method:result.method||label,sourceUrl:result.sourceUrl||sourceUrl,
    raw:String(result.rawText||'').slice(0,12000),
    message:[result.message,warnings].filter(Boolean).join(' · ')});
  return true;
}
function v42WarnNoResult(url,result) {
  browserPendingSourceURL=url;
  const preview='<p>Le serveur a essayé, mais cette source ne contient pas de recette détectable. Vous pouvez continuer en ajoutant une vidéo ou en collant sa légende.</p>';
  openModal('Extraction incomplète','Cette source reste accessible comme lien.',`${preview}
     <p class="bv4-tip">${escapeHTML(result?.message||'Aucune recette détectable.')}</p>
     <button class="btn" data-bv4-action="continue-video" type="button">Importer le fichier vidéo ou sa transcription →</button>`,{wide:true});
}

// These are function bindings used by the original submission listeners.
v2ImportURL = async function v42ImportURL(form) {
  const submitted=String(new FormData(form).get('url')||'').trim();
  let parsed,url;
  try {parsed=new URL(submitted);if(!/^https?:$/.test(parsed.protocol))throw Error('Lien invalide');url=bv4NormalizeSource(submitted);}
  catch {bv4Status('Indiquez un lien https:// valide.','error','bv4-url-status');return;}
  const button=form.querySelector('[type="submit"]');
  const oldLabel=button?.textContent;
  if(button){button.disabled=true;button.textContent='Extraction sur le serveur…';}
  bv4Status('Le serveur consulte la source. Si la légende est incomplète, il essaie les sous-titres, la voix et les images. Cela peut durer quelques minutes…','loading','bv4-url-status');
  try {
    const result=await v42ServerCall('/api/extract',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({url})});
    if(!v42ReviewResult(result,{sourceUrl:url})) v42WarnNoResult(url,result);
    else browserPendingSourceURL='';
  } catch(error) {
    bv4Status('Extraction automatique impossible : '+error.message+' · Vous pouvez importer le fichier vidéo ou coller la transcription.','warning','bv4-url-status');
    browserPendingSourceURL=url;
    const status=document.getElementById('bv4-url-status');
    if(status && !document.getElementById('v42-fallback'))status.insertAdjacentHTML('afterend',
      '<button id="v42-fallback" data-bv4-action="continue-video" type="button" class="btn btn-secondary">Utiliser une vidéo locale ou une transcription →</button>');
  } finally {if(button?.isConnected){button.disabled=false;button.textContent=oldLabel;}}
};

bv4ImportMedia = async function v42ImportMedia(form){
  if(browserBusy)return;
  const media=form.querySelector('[name="media"]')?.files?.[0];
  const fd=new FormData(form);
  const notes=String(fd.get('notes')||'').trim();
  const sourceUrl=String(fd.get('sourceUrl')||browserPendingSourceURL||'').trim();
  if(!media && notes){
    bv4Review(bv4RecipeFromText(notes,'Recette depuis une transcription'),
      {method:'Transcription collée',sourceUrl,raw:notes,message:'Texte collé, vérifiez les étapes.'});return;
  }
  if(!media){bv4Status('Choisissez un fichier vidéo/audio ou collez la transcription.','warning');return;}
  const button=form.querySelector('[type="submit"]');
  const oldLabel=button.textContent;
  browserBusy=true;button.disabled=true;button.textContent='Analyse serveur…';
  bv4Status('Vidéo envoyée au serveur : transcription Whisper et lecture OCR. Ce traitement peut prendre plusieurs minutes…');
  try {
    if(media.size>100*1024*1024)throw Error('Fichier supérieur à 100 Mo.');
    const body=new FormData();body.append('media',media,media.name);
    const result=await v42ServerCall('/api/media',{method:'POST',body});
    if(notes && result.status!=='none') {
      const supplemental=bv4RecipeFromText(notes,media.name);
      const recipe=result.recipe;
      recipe.ingredients=[...new Set([...(recipe.ingredients||[]),...supplemental.ingredients])];
      recipe.steps=[...new Set([...(recipe.steps||[]),...supplemental.steps])];
      result.message=(result.message||'')+' · Texte complémentaire ajouté.';
    }
    if(!v42ReviewResult(result,{sourceUrl,label:'Vidéo / Audio'})){
      if(notes){bv4Review(bv4RecipeFromText(notes,media.name),
        {method:'Transcription collée',sourceUrl,raw:notes,message:result.message||'Aucune recette détectée dans le média.'});}
      else bv4Status((result.message||'Aucune recette détectée.')+' '+(result.warnings||[]).join(' · '),'warning');
    }
    if(result.status!=='none')browserPendingSourceURL='';
  } catch(error){bv4Status('Impossible d’analyser ce média : '+error.message,'error');}
  finally {browserBusy=false;if(button.isConnected){button.disabled=false;button.textContent=oldLabel;}}
};

bv4ImportImage = async function v42ImportPhoto(form){
  const photo=form.querySelector('[name="image"]')?.files?.[0];if(!photo)return;
  const button=form.querySelector('[type="submit"]');const label=button?.textContent;
  if(button){button.disabled=true;button.textContent='Lecture OCR…';}
  bv4Status('Lecture de l’image côté serveur…');
  try{
    const data=new FormData();data.append('image',photo,photo.name);
    const output=await v42ServerCall('/api/ocr',{method:'POST',body:data});
    if(!v42ReviewResult(output,{label:'Photo OCR'}))bv4Status('Aucune recette détectée sur la photo.','warning');
  }catch(error){bv4Status('Photo non lisible : '+error.message,'error');}
  finally{if(button?.isConnected){button.disabled=false;button.textContent=label;}}
};

const v42OriginalRender = renderImport;
renderImport = function(){
  v42OriginalRender();
  const modal=document.querySelector('#modal-root .modal-body');if(!modal)return;
  if(modalMode==='url'){
    const callout=modal.querySelector('.inline-callout');
    if(callout)callout.innerHTML='<strong>✳ Extraction en ligne</strong><br>Collez Instagram Reel, TikTok, YouTube ou un site de recettes. Cooknote tente de lire la légende, les sous-titres, la voix et les images accessibles.';
    const hint=modal.querySelector('#import-url')?.parentElement?.querySelector('.field-hint');
    if(hint)hint.textContent='Serveur en ligne requis. Certains Reels privés ou protégés refusent automatiquement l’accès.';
    const tip=modal.querySelector('.bv4-tip');
    if(tip)tip.innerHTML='✳ <strong>V4.2 en ligne.</strong> Si Instagram bloque le serveur, une option d’import manuel reste disponible.';
    const button=modal.querySelector('#add-url-form button[type="submit"]');if(button)button.textContent='Extraire la recette automatiquement ↗';
    const field=modal.querySelector('#import-url');if(field)field.placeholder=V42_EXAMPLE;
  }
  if(modalMode==='media'){
    const hint=modal.querySelector('#bv4-media-form .bv4-tip');
    if(hint)hint.textContent='Le fichier est transmis à votre serveur pour analyse. Les données sont supprimées du serveur après traitement.';
  }
};
// Disambiguate the version. Your browser retains all recipes from V4.1.
document.querySelector('.side-identity small')?.replaceChildren(document.createTextNode('V4.2 en ligne · recettes locales'));
const mark=document.querySelector('.topbar .local-pill');if(mark)mark.title='Les recettes sont conservées dans ce navigateur. Les médias sont traités sur le serveur.';