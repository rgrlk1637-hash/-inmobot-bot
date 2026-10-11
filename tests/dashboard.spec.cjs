// Navegador nuevo y sin sesiones. Red completamente interceptada; nunca publica en Meta.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
const fixture=path.join(root,'analisis/qa-galeria/integracion');
const original=JSON.parse(fs.readFileSync(path.join(fixture,'dashboard.json'),'utf8'));

(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let state=structuredClone(original),calls=[],failInstagram=false;
    state.perfil.fb_conectado=true;
    state.perfil.pages=[{id:'first',name:'Primera página de prueba'},{id:'chosen',name:'Página elegida de prueba'}];
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());
      assert.equal(url.hostname,'inmobot.test','El navegador intentó salir de la prueba local');
      if(url.pathname==='/api/dashboard')return route.fulfill({json:state});
      if(url.pathname==='/api/publish'){
        const body=route.request().postDataJSON();calls.push(body);
        if(failInstagram&&body.destination==='ig_feed')return route.fulfill({json:{success:false,error:'Fallo simulado de Instagram'}});
        const exists=state.avisos[0].pubs.some(p=>p.destino===body.destination);
        if(!exists)state.avisos[0].pubs.push({destino:body.destination,likes:0,comments:0,shares:0,fecha:'Prueba',mes:state.mes});
        return route.fulfill({json:{success:true,post_id:'simulated',already_published:exists}});
      }
      if(url.pathname==='/dashboard')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'static/dashboard.html'))});
      if(url.pathname.startsWith('/media/'))return route.fulfill({contentType:'image/jpeg',body:fs.readFileSync(path.join(fixture,'media',path.basename(url.pathname)))});
      if(url.pathname==='/logo')return route.fulfill({contentType:'image/png',body:fs.readFileSync(path.join(root,'static','logo.png'))});
      return route.fulfill({status:404,body:''});
    });
    await page.goto('http://inmobot.test/dashboard?tid=123&k=test-key');
    const card=page.locator('.aviso').first();await card.waitFor();
    assert.equal(await card.locator('[data-photo-index]').count(),7);
    await page.waitForFunction(()=>Array.from(document.querySelectorAll('.galeria img')).every(i=>i.complete&&i.naturalWidth>0));
    for(let i=1;i<7;i++)await card.locator('[data-photo-step="1"]').click();
    assert.equal(await card.locator('.contador').innerText(),'7 / 7');
    assert.equal(await card.locator('[data-photo-step="1"]').isDisabled(),true);
    await card.locator('[data-photo-index="2"]').click();
    assert.equal(await card.locator('.contador').innerText(),'3 / 7');
    assert.equal(await card.locator('.foto-grande').getAttribute('href'),state.avisos[0].images[2]);
    await card.locator('[data-photo-index="0"]').click();
    await page.screenshot({path:path.join(fixture,'panel-escritorio.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'Desborde horizontal en celular');
    await card.screenshot({path:path.join(fixture,'galeria-celular.png')});
    // La elección explícita evita publicar en la primera página por accidente.
    await card.locator('[data-pub="both"]').click();
    assert.equal(calls.length,0);
    await card.locator('.pagina').selectOption('chosen');
    failInstagram=true;
    await card.locator('[data-pub="both"]').click();
    await page.waitForFunction(()=>document.querySelector('.msg').textContent.includes('Fallo simulado'));
    assert.deepEqual(calls.map(c=>c.destination),['fb_feed','ig_feed']);
    assert(calls.every(c=>c.page_id==='chosen'));
    assert.equal(await card.locator('[data-pub="fb_feed"]').isDisabled(),true);
    assert.equal(await card.locator('[data-pub="ig_feed"]').isDisabled(),false);
    assert((await card.locator('.msg').innerText()).includes('Facebook: publicado.'));
    failInstagram=false;
    await card.locator('[data-pub="ig_feed"]').click();
    await page.waitForFunction(()=>document.querySelector('.msg').textContent==='Instagram: publicado.');
    assert.equal(calls.length,3);
    assert.equal(calls[2].destination,'ig_feed');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(fixture,'navegador.json'),JSON.stringify({fotos:7,escritorio:true,celular:true,
      navegacion_galeria:true,seleccion_pagina:true,ambas_redes_simuladas:true,reintento_solo_red_fallida:true,
      errores_javascript:errors,publicaciones_reales:0},null,2));
    console.log('Panel verificado: 7 fotos, escritorio y celular, ambas redes y recuperación parcial simuladas.');
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
