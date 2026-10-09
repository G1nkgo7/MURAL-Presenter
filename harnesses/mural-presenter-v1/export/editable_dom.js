/* 只读取 Chromium 已完成的布局，不计算 CSS 布局；不能原生表达的局部显式截图。 */
({width, height}) => {
  const candidates = [...document.querySelectorAll('.slide[data-slide], .mural-slide, .slide')];
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.display==='none' || s.visibility==='hidden' || s.contentVisibility==='hidden') return false;
    for (let parent=el;parent;parent=parent.parentElement) if (Number(getComputedStyle(parent).opacity)===0) return false;
    return el.getClientRects().length>0 || s.display==='contents';
  };
  const root = candidates.find(el => visible(el) && Math.abs(el.getBoundingClientRect().width-width)<2 && Math.abs(el.getBoundingClientRect().height-height)<2) || document.body;
  const rr = root.getBoundingClientRect();
  if (Math.abs(rr.width-width)>2 || Math.abs(rr.height-height)>2) throw Error('editable canvas geometry mismatch');
  const elements = [root, ...root.querySelectorAll('*')];
  elements.forEach((el, i) => el.setAttribute('data-mural-export-id', String(i)));
  const rows = [], covered = new Set(), texts = [], canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d');
  const color = value => {
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1);
    return [...ctx.getImageData(0, 0, 1, 1).data];
  };
  const box = r => ({x:r.x-rr.x, y:r.y-rr.y, w:r.width, h:r.height});
  const selector = el => {
    const parts = [];
    while (el && el !== root) {
      parts.unshift(`${el.tagName.toLowerCase()}:nth-child(${[...el.parentElement.children].indexOf(el)+1})`);
      el = el.parentElement;
    }
    return ['slide', ...parts].join(' > ');
  };
  const id = el => el.getAttribute('data-mural-export-id');
  const emit = (el, row) => rows.push({dom_id:id(el), selector:selector(el), ...row});
  const textStyle = el => {
    const s = getComputedStyle(el);
    return {font:s.fontFamily, size:parseFloat(s.fontSize), bold:Number(s.fontWeight)>=600,
      weight:Number(s.fontWeight), italic:s.fontStyle==='italic', underline:s.textDecorationLine.includes('underline'),
      strike:s.textDecorationLine.includes('line-through'), color:color(s.color),
      letter_spacing:parseFloat(s.letterSpacing)||0, transform:s.textTransform, opacity:Number(s.opacity)};
  };
  const content = node => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const found = [];
    while (walker.nextNode()) {
      const n = walker.currentNode;
      if (n.data.trim() && visible(n.parentElement) && !n.parentElement.closest('script,style,noscript,template')) found.push(n);
    }
    return found;
  };
  const effectInset = s => {
    let extent = 0;
    for (const shadow of [s.boxShadow, s.textShadow]) {
      const clean = shadow.replace(/rgba?\([^)]*\)/g, '');
      for (const part of clean.split(',')) {
        const values = [...part.matchAll(/(-?[\d.]+)px/g)].map(m => Number(m[1]));
        if (values.length>=2) extent = Math.max(extent, Math.abs(values[0])+Math.abs(values[1])+2*(values[2]||0)+Math.abs(values[3]||0));
      }
    }
    for (const m of s.filter.matchAll(/blur\(([\d.]+)px\)/g)) extent = Math.max(extent, 3*Number(m[1]));
    extent=Math.max(extent,parseFloat(s.outlineWidth)+Math.max(0,parseFloat(s.outlineOffset)));
    return Math.ceil(extent);
  };
  const pseudoPaint = el => ['::before','::after'].some(p => {
    const s = getComputedStyle(el, p);
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && s.content !== 'none' && s.content !== 'normal' &&
      (s.content !== '""' || s.backgroundImage !== 'none' || color(s.backgroundColor)[3] || s.boxShadow!=='none' || s.filter!=='none' ||
        ['Top','Right','Bottom','Left'].some(side=>parseFloat(s[`border${side}Width`])>0));
  });
  const complex = (el, s) => {
    const reasons = [];
    if (['svg','canvas','video','iframe','object','embed','input','textarea','select'].includes(el.tagName.toLowerCase())) reasons.push(el.tagName.toLowerCase());
    if (['UL','OL'].includes(el.tagName) && [...el.children].some(child=>getComputedStyle(child).listStyleType!=='none')) reasons.push('list-marker');
    if (s.transform !== 'none' && !/^matrix\(1, 0, 0, 1, (?:0|[-\d.]+), (?:0|[-\d.]+)\)$/.test(s.transform)) reasons.push('transform');
    if (s.filter !== 'none') reasons.push('filter');
    if (s.backdropFilter && s.backdropFilter !== 'none') reasons.push('backdrop-filter');
    if (s.mixBlendMode !== 'normal') reasons.push('mix-blend-mode');
    if (s.clipPath !== 'none' || s.maskImage !== 'none') reasons.push('clip-or-mask');
    if (s.writingMode !== 'horizontal-tb') reasons.push('vertical-writing');
    if (s.fontVariantCaps!=='normal' || s.hyphens==='auto' || parseFloat(s.webkitTextStrokeWidth)>0) reasons.push('special-text-rendering');
    if (s.textDecorationLine!=='none' && s.textDecorationStyle!=='solid') reasons.push('text-decoration-style');
    if (s.textShadow !== 'none') reasons.push('text-shadow');
    if (s.backgroundClip === 'text' || s.webkitBackgroundClip === 'text') reasons.push('gradient-text');
    if (Number(s.opacity)<1 && el.children.length) reasons.push('group-opacity');
    if ((s.overflowX !== 'visible' || s.overflowY !== 'visible') && el !== root &&
        (el.scrollWidth>el.clientWidth+1 || el.scrollHeight>el.clientHeight+1)) reasons.push('clipped-content');
    if (s.overflow==='hidden' && parseFloat(s.borderRadius)>0 && el.children.length) reasons.push('rounded-overflow');
    if (pseudoPaint(el) && ['::before','::after'].some(p=>{
      const ps=getComputedStyle(el,p);
      return ps.content!=='none' && ps.content!=='normal' && (ps.content!=='""' || !(Number(ps.zIndex)<0));
    })) reasons.push('pseudo-element-composite');
    return reasons;
  };
  const fallback = (el, reasons, decoration=false) => {
    const s = getComputedStyle(el), r = box(el.getBoundingClientRect());
    const nodes = decoration ? [] : content(el);
    nodes.forEach(n => covered.add(n));
    const pseudo_insets={};
    if (pseudoPaint(el)) for (const name of ['before','after']) pseudo_insets[name]=effectInset(getComputedStyle(el,`::${name}`));
    const capture_box={...r};
    if (!decoration) for (const child of el.querySelectorAll('*')) {
      if (!visible(child)) continue;
      const childBox=box(child.getBoundingClientRect()), inset=effectInset(getComputedStyle(child));
      const right=Math.max(capture_box.x+capture_box.w,childBox.x+childBox.w+inset);
      const bottom=Math.max(capture_box.y+capture_box.h,childBox.y+childBox.h+inset);
      capture_box.x=Math.min(capture_box.x,childBox.x-inset);capture_box.y=Math.min(capture_box.y,childBox.y-inset);
      capture_box.w=right-capture_box.x;capture_box.h=bottom-capture_box.y;
    }
    emit(el, {type:'raster', box:r, capture_box, reasons, decoration, inset:effectInset(s), pseudo_insets,
      text: nodes.map(n=>n.data).join(' '), is_root:el===root});
  };
  const nativeText = node => {
    if (!node.data.trim()) return;
    const el = node.parentElement, s = getComputedStyle(el), style = textStyle(el);
    const lines = [];
    for (let i=0;i<node.data.length;) {
      const end = i + (node.data.codePointAt(i)>0xffff ? 2 : 1);
      const range = document.createRange(); range.setStart(node,i); range.setEnd(node,end);
      const rect = range.getBoundingClientRect(), value = node.data.slice(i,end); i=end;
      if (rect.width<0.01 || rect.height<0.01) continue;
      const r = box(rect);
      if (r.x+r.w<=0 || r.y+r.h<=0 || r.x>=width || r.y>=height) continue;
      const last = lines[lines.length-1];
      if (last && Math.abs(last.box.y-r.y)<1 && Math.abs(last.box.h-r.h)<1 && r.x>=last.box.x-0.5) {
        last.text += value; last.box.w = Math.max(last.box.w, r.x+r.w-last.box.x);
      } else lines.push({text:value, box:r});
    }
    if (!lines.length) return;
    covered.add(node);
    for (const line of lines) {
      if (!line.text.trim()) continue;
      if (s.whiteSpace==='normal' || s.whiteSpace==='nowrap') line.text=line.text.replace(/[\t\n\r ]+/g,' ');
      if (style.transform==='uppercase') line.text=line.text.toLocaleUpperCase();
      if (style.transform==='lowercase') line.text=line.text.toLocaleLowerCase();
      if (style.transform==='capitalize') line.text=line.text.replace(/\b\p{L}/gu, c=>c.toLocaleUpperCase());
      emit(el, {type:'text', ...line, style});
    }
  };
  const table = el => {
    if (el.querySelector('table,img,svg,canvas,video')) return false;
    const cells = [...el.querySelectorAll('th,td')];
    if (!cells.length) return false;
    for (const part of [el,...el.querySelectorAll('*')]) {
      if (!visible(part)) continue;
      const s=getComputedStyle(part), cell=['TD','TH'].includes(part.tagName);
      if (complex(part,s).length || pseudoPaint(part) || s.backgroundImage!=='none' || s.boxShadow!=='none' ||
          (!cell && color(s.backgroundColor)[3]) || ['Top','Right','Bottom','Left'].some(side=>
            parseFloat(s[`border${side}Width`])>0 && (!cell || s[`border${side}Style`]!=='solid'))) return false;
    }
    const records = cells.map(cell => {
      const s = getComputedStyle(cell), nodes=content(cell);
      nodes.forEach(n=>covered.add(n));
      return {dom_id:id(cell), box:box(cell.getBoundingClientRect()), text:cell.innerText,
        runs:nodes.map(n=>({dom_id:id(n.parentElement),text:n.data.replace(/\s+/g,' '), style:textStyle(n.parentElement)})),
        style:textStyle(cell), fill:color(s.backgroundColor), align:s.textAlign,
        valign:s.verticalAlign, padding:[s.paddingTop,s.paddingRight,s.paddingBottom,s.paddingLeft].map(parseFloat),
        borders:['Top','Right','Bottom','Left'].map(side=>({width:parseFloat(s[`border${side}Width`]),color:color(s[`border${side}Color`])}))};
    });
    emit(el, {type:'table', box:box(el.getBoundingClientRect()), cells:records});
    return true;
  };
  const walk = el => {
    if (!visible(el) || el.closest('script,style,noscript,template')) return;
    const s = getComputedStyle(el), r = box(el.getBoundingClientRect());
    const reasons=complex(el,s);
    if (reasons.includes('backdrop-filter') || reasons.includes('mix-blend-mode')) throw Error(`editable unsupported cross-element compositing: ${selector(el)}`);
    if (reasons.length) { fallback(el,reasons); return; }
    if (el.tagName==='TABLE') {
      if (!table(el)) fallback(el,['complex-table']);
      return;
    }
    const decoration=[];
    if (s.backgroundImage!=='none') decoration.push('background-image-or-gradient');
    if (s.boxShadow!=='none') decoration.push('box-shadow');
    if (s.outlineStyle!=='none' && parseFloat(s.outlineWidth)>0) decoration.push('outline');
    if (pseudoPaint(el)) decoration.push('pseudo-element');
    const sides=['Top','Right','Bottom','Left'].map(side=>({width:parseFloat(s[`border${side}Width`]),style:s[`border${side}Style`],color:color(s[`border${side}Color`])}));
    const radii=[s.borderTopLeftRadius,s.borderTopRightRadius,s.borderBottomRightRadius,s.borderBottomLeftRadius];
    if (new Set(radii).size>1 || radii.some(x=>x.includes(' '))) decoration.push('asymmetric-border-radius');
    if (sides.some(b=>b.width && !['solid','none'].includes(b.style))) decoration.push('border-style');
    if (decoration.length) fallback(el,decoration,true);
    else {
      const fill=color(s.backgroundColor), uniform=sides.every(b=>JSON.stringify(b)===JSON.stringify(sides[0]));
      const radius=parseFloat(radii[0])*(radii[0].includes('%')?Math.min(r.w,r.h)/100:1);
      const ellipse=radii.every(value=>value==='50%') || (Math.abs(r.w-r.h)<1 && radius>=r.w/2);
      if (r.w>0 && r.h>0 && (fill[3] || sides.some(b=>b.width))) emit(el,{type:'shape',box:r,fill,borders:sides,radius,ellipse,uniform,opacity:Number(s.opacity)});
    }
    if (el.tagName==='IMG') {
      if (!el.complete || !el.naturalWidth) throw Error('editable image failed to load');
      if (!/^data:image\/(png|jpeg);/i.test(el.currentSrc||el.src)) fallback(el,['image-format']);
      else if (s.objectFit!=='fill' || Number(s.opacity)!==1 || radii.some(v=>parseFloat(v)>0)) fallback(el,['image-fit-opacity-or-radius']);
      else emit(el,{type:'image',box:r,src:el.currentSrc||el.src,opacity:Number(s.opacity)});
      return;
    }
    for (const node of el.childNodes) {
      if (node.nodeType===Node.TEXT_NODE) nativeText(node);
      else if (node.nodeType===Node.ELEMENT_NODE) walk(node);
    }
  };
  const ancestors=[];
  for (let el=root.parentElement;el;el=el.parentElement) ancestors.unshift(el);
  let backdrop=[255,255,255,255], complexBackdrop=false;
  for (const el of ancestors) {
    const s=getComputedStyle(el), c=color(s.backgroundColor), a=c[3]/255;
    backdrop=backdrop.map((v,i)=>i===3?255:Math.round(c[i]*a+v*(1-a)));
    if (s.backgroundImage!=='none') complexBackdrop=true;
  }
  if (complexBackdrop) {
    fallback(root,['ancestor-background'],true);
    rows[rows.length-1].keep_ancestor_background=true;
  } else {
    emit(root,{type:'shape',box:{x:0,y:0,w:width,h:height},fill:backdrop,radius:0,ellipse:false,uniform:true,opacity:1,
      borders:Array.from({length:4},()=>({width:0,color:[0,0,0,0],style:'none'}))});
  }
  walk(root);
  for (const node of content(root)) {
    const range=document.createRange();range.selectNodeContents(node);
    const r=box(range.getBoundingClientRect());
    if (r.w>0 && r.h>0 && r.x+r.w>0 && r.y+r.h>0 && r.x<width && r.y<height) {
      texts.push(node.data);
      if (!covered.has(node)) throw Error(`editable unaccounted visible text: ${selector(node.parentElement)}`);
    }
  }
  return {rows, root_id:id(root), root_box:{x:rr.x,y:rr.y,w:rr.width,h:rr.height}, visible_text_nodes:covered.size};
}
