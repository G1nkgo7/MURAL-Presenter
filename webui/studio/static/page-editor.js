/* 可视画布只用于交互；保存时向原始 HTML 回写增量，并更新正式 PNG。 */
(() => {
  "use strict";
  let session = null;
  const NON_OBJECT = "script,style,link,meta,base,title,desc,defs,stop,linearGradient,radialGradient,clipPath,mask,pattern,source";
  const EDIT_STYLES = ["transform","width","height","font-size","color","-webkit-text-fill-color","opacity","white-space"];
  const q = (selector) => session.dialog.querySelector(selector);
  const round = (value) => Math.round(value * 100) / 100;
  const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  async function loadSource(deckId, page) {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${deckId}/pages/${page}/edit`, {credentials:"same-origin", signal:AbortSignal.timeout(20000)});
    const source = await response.json();
    if (!response.ok) throw new Error(source.detail || "原页面暂不能编辑。");
    const frame = document.createElement("iframe");
    frame.setAttribute("sandbox","allow-scripts allow-same-origin");
    frame.style.cssText="position:fixed;left:-20000px;top:0;width:1600px;height:900px;visibility:hidden;pointer-events:none";
    frame.title="准备原页面编辑";
    document.body.append(frame);
    try {
      await new Promise((resolve,reject) => {
        const timeout=setTimeout(()=>reject(new Error("原页面资源加载超时，尚未修改源码。")),20000);
        frame.addEventListener("load",()=>{clearTimeout(timeout);resolve();},{once:true});
        frame.srcdoc=source.html;
      });
      await frame.contentDocument.fonts.ready;
      await new Promise(resolve=>frame.contentWindow.requestAnimationFrame(()=>frame.contentWindow.requestAnimationFrame(resolve)));
      return {...capture(pageRoot(frame, page)), version:source.version};
    } finally { frame.remove(); }
  }

  function pageRoot(frame, number) {
    const doc = frame?.contentDocument;
    if (!doc || doc.readyState !== "complete") throw new Error("页面 HTML 尚未加载完成，请稍后再试。");
    const shell = [...doc.querySelectorAll(".slide-shell[data-slide]")].find(el => Number(el.dataset.slide) === number);
    const nested = shell?.querySelector("iframe") || doc.querySelector(`#wrap > iframe:nth-child(${number})`);
    if (nested) return pageRoot(nested, number);
    const root = [...doc.querySelectorAll(".slide[data-slide]")].find(el => Number(el.dataset.slide) === number)
      || (doc.querySelectorAll(".slide").length === 1 ? doc.querySelector(".slide") : null)
      || doc.querySelectorAll(".slide")[number - 1]
      || doc.querySelector("#poster")
      || (!doc.querySelector(".slide,.slide-shell,iframe") ? doc.body : null);
    if (!root || root.getBoundingClientRect().width < 1 || root.getBoundingClientRect().height < 1)
      throw new Error("当前页没有可编辑的 HTML；PNG 预览不能拆成文字和图层。");
    return root;
  }

  function absoluteCSS(css, base) {
    return css.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/g, (match, a, b, c) => {
      const raw = (a ?? b ?? c).trim();
      if (!raw || raw.startsWith("#") || raw.startsWith("data:")) return match;
      const url = new URL(raw, base);
      if (url.hash && url.href.split("#")[0] === base.split("#")[0]) return `url("${url.hash}")`;
      return `url("${url.href}")`;
    });
  }

  function resolvedStyle(computed, base) {
    const style = document.createElement("span").style;
    for (const name of computed) {
      if (!name.startsWith("--")) style.setProperty(name, absoluteCSS(computed.getPropertyValue(name), base));
    }
    style.setProperty("animation", "none", "important");
    style.setProperty("transition", "none", "important");
    return style.cssText;
  }

  function capture(root) {
    const doc = root.ownerDocument, win = doc.defaultView;
    if (root.querySelector("iframe,object,embed,video"))
      throw new Error("当前页含嵌入网页或视频，暂不能直接编辑。可继续使用文字指令修改。");
    const clone = root.cloneNode(true), originals = [root, ...root.querySelectorAll("*")];
    const copies = [clone, ...clone.querySelectorAll("*")], fonts = new Set(), rules = [];
    let canvasCount = 0;
    originals.forEach((node, index) => {
      let copy = copies[index];
      if (node.matches(NON_OBJECT)) return;
      const cs = win.getComputedStyle(node);
      cs.fontFamily.split(",").forEach((family) => fonts.add(family.trim().replace(/^['"]|['"]$/g, "").toLowerCase()));
      copy.setAttribute("style", resolvedStyle(cs, doc.baseURI));
      copy.setAttribute("data-mural-node", String(index));
      for (const pseudo of ["::before", "::after"]) {
        const ps = win.getComputedStyle(node, pseudo);
        if (ps.content && !["none", "normal"].includes(ps.content))
          rules.push(`[data-mural-node="${index}"]${pseudo}{${resolvedStyle(ps, doc.baseURI)}}`);
      }
      if (node.tagName === "CANVAS") {
        const image = doc.createElement("img");
        for (const attr of copy.attributes) image.setAttribute(attr.name, attr.value);
        try { image.src = node.toDataURL("image/png"); }
        catch { throw new Error("当前页图表无法读取像素，未创建不完整的编辑草稿。"); }
        image.alt = "图表当前画面（静态图像）";
        copy.replaceWith(image); copy = image; canvasCount++;
      } else if (node.tagName === "IMG") {
        if (!node.complete || !node.naturalWidth) throw new Error("当前页仍有图片未加载，请等图片完整显示后再编辑。");
        copy.setAttribute("src", node.currentSrc || node.src);
        copy.removeAttribute("srcset"); copy.removeAttribute("sizes");
      }
      for (const attr of [...copy.attributes]) {
        if (/^on/i.test(attr.name) || ["contenteditable", "autofocus", "srcdoc", "action", "formaction"].includes(attr.name)) copy.removeAttribute(attr.name);
        else if (["href", "xlink:href"].includes(attr.name) && !attr.value.startsWith("#")) {
          const url = new URL(attr.value, doc.baseURI);
          if (["http:", "https:"].includes(url.protocol) || (url.protocol === "data:" && node.namespaceURI === "http://www.w3.org/2000/svg")) copy.setAttribute(attr.name, url.href);
          else copy.removeAttribute(attr.name);
        }
      }
    });
    clone.querySelectorAll("script,style,link,meta,base,source").forEach((node) => node.remove());
    const collectFonts = (sheets) => {
      for (const sheet of sheets) {
        let entries;
        try { entries = sheet.cssRules; }
        catch { throw new Error("页面含不可读取的外部样式，无法保证编辑字体一致；请改用已打包的页面。"); }
        for (const rule of entries || []) {
          if (rule.type === 3 && rule.styleSheet) collectFonts([rule.styleSheet]);
          else if (rule.type === 5 && fonts.has(rule.style.getPropertyValue("font-family").replace(/['"]/g, "").trim().toLowerCase()))
            rules.push(absoluteCSS(rule.cssText, sheet.href || doc.baseURI));
          else if (rule.cssRules) collectFonts([{cssRules: rule.cssRules, href: sheet.href}]);
        }
      }
    };
    collectFonts(doc.styleSheets);
    const width = root.offsetWidth, height = root.offsetHeight;
    if (!(width > 0 && height > 0)) throw new Error("无法读取当前页的实际画布尺寸。");
    Object.assign(clone.style, {position:"relative", inset:"auto", margin:"0", width:`${width}px`, height:`${height}px`,
      minWidth:"0", minHeight:"0", maxWidth:"none", maxHeight:"none", boxSizing:"border-box", transform:"none", translate:"none", rotate:"none", scale:"none", opacity:"1", visibility:"visible", pointerEvents:"auto"});
    clone.setAttribute("data-mural-page", "");
    clone.removeAttribute("aria-hidden");
    // body 作为画布时也保持单一 body，而不是依赖 HTML 解析器纠正嵌套 body。
    const markup = clone.tagName === "BODY" ? `<div ${[...clone.attributes].map(a => `${a.name}="${escape(a.value)}"`).join(" ")}>${clone.innerHTML}</div>` : clone.outerHTML;
    const css = `html,body{margin:0;width:${width}px;height:${height}px;overflow:hidden;background:${win.getComputedStyle(doc.body).backgroundColor}}\n${rules.join("\n")}`;
    return {width, height, canvasCount, css, html:`<!doctype html><html lang="${escape(doc.documentElement.lang || "zh")}"><head><meta charset="utf-8"><style>${css.replace(/<\/style/gi,"<\\/style")}</style></head><body>${markup}</body></html>`};
  }

  function isText(el) {
    return el && el.namespaceURI === "http://www.w3.org/1999/xhtml" && !el.matches("img,canvas,br,hr")
      && [...el.children].every(child => child.tagName === "BR");
  }
  function objects() {
    return [...session.root.querySelectorAll("[data-mural-source]")].filter(el => !el.matches(NON_OBJECT)
      && !el.closest("svg defs,svg clipPath,svg mask") && (!el.closest("svg") || el.tagName.toLowerCase() === "svg"));
  }
  function label(el) { return (el.getAttribute("aria-label") || el.dataset.name || el.getAttribute("alt") || el.textContent.trim() || el.id || el.tagName.toLowerCase()).replace(/\s+/g," ").slice(0,60); }
  function state(el) { return {style:el.getAttribute("style"), x:el.dataset.muralX, y:el.dataset.muralY, r:el.dataset.muralR,
    text:isText(el) ? el.innerHTML : null, src:el.tagName === "IMG" ? el.getAttribute("src") : null}; }
  function restore(el, value) {
    el.setAttribute("style", value.style || "");
    for (const key of ["x","y","r"]) {
      if (value[key] === undefined) delete el.dataset[`mural${key.toUpperCase()}`];
      else el.dataset[`mural${key.toUpperCase()}`] = value[key];
    }
    if (value.text !== null) el.innerHTML = value.text;
    if (value.src !== null) el.setAttribute("src", value.src);
  }
  function remember(undo, redo) {
    session.history.push({undo, redo});
    if (session.history.length > 40) session.history.shift();
    session.future.length = 0; session.dirty = true; refresh();
  }
  function change(action) {
    if (session.busy) return;
    const el = session.selected;
    if (!el) return;
    const before = state(el); action(el); const after = state(el);
    if (JSON.stringify(before) !== JSON.stringify(after)) remember(() => restore(el,before), () => restore(el,after));
    else refresh();
  }
  function travel(redo = false) {
    if (session.busy) return;
    const from = redo ? session.future : session.history, to = redo ? session.history : session.future;
    const entry = from.pop(); if (!entry) return;
    entry[redo ? "redo" : "undo"](); to.push(entry); session.dirty = true; refresh();
  }
  function select(el) {
    session.selected = el && el !== session.root ? el : null;
    refresh();
  }
  function move(el, x, y, r) {
    if (!session.transforms.has(el)) session.transforms.set(el, el.style.transform === "none" ? "" : el.style.transform);
    Object.assign(el.dataset, {muralX:String(x), muralY:String(y), muralR:String(r)});
    el.style.setProperty("transform", `translate(${x}px,${y}px) rotate(${r}deg) ${session.transforms.get(el)}`, "important");
  }
  function remove() {
    if (session.busy) return;
    const el = session.selected; if (!el) return;
    const parent = el.parentNode, next = el.nextSibling;
    el.remove();
    remember(() => parent.insertBefore(el, next?.parentNode === parent ? next : null), () => el.remove());
  }
  function message(text) { q("[data-editor-status]").textContent = text; }

  function outline() {
    const el = session.selected, box = q(".page-edit-selection");
    box.hidden = !el?.isConnected;
    if (box.hidden) return;
    const rect = el.getBoundingClientRect(), scale = session.scale;
    const frame = session.frame.getBoundingClientRect(), host = q(".page-edit-stage").getBoundingClientRect();
    Object.assign(box.style, {left:`${frame.left-host.left+rect.left*scale}px`,top:`${frame.top-host.top+rect.top*scale}px`,
      width:`${rect.width*scale}px`,height:`${rect.height*scale}px`});
  }
  function refresh() {
    if (!session?.root) return;
    if (!session.selected?.isConnected) session.selected = null;
    const el = session.selected;
    q("[data-editor-undo]").disabled = !session.history.length;
    q("[data-editor-redo]").disabled = !session.future.length;
    q("[data-editor-properties]").hidden = !el;
    q("[data-editor-empty]").hidden = !!el;
    q("[data-editor-changed]").textContent = session.dirty ? "有未保存修改" : "原页面";
    if (el) {
      const cs = session.frame.contentWindow.getComputedStyle(el);
      q("[data-editor-name]").textContent = label(el);
      for (const key of ["x","y","r"]) q(`[data-prop="${key}"]`).value = round(Number(el.dataset[`mural${key.toUpperCase()}`] || 0));
      for (const key of ["width","height","font-size","opacity"]) {
        const input = q(`[data-prop="${key}"]`);
        input.value = round(parseFloat(cs.getPropertyValue(key)) || 0);
        input.disabled = key === "font-size" && !isText(el);
      }
      q("[data-editor-text]").hidden = !isText(el);
      q("[data-prop=text]").value = isText(el) ? el.innerText : "";
      const rgb = cs.color.match(/\d+/g)?.slice(0,3) || [0,0,0];
      q("[data-prop=color]").value = "#" + rgb.map(v => Number(v).toString(16).padStart(2,"0")).join("");
      q("[data-prop=color]").disabled = !isText(el);
      q("[data-editor-replace]").hidden = el.dataset.muralSourceTag !== "img";
      q("[data-editor-parent]").disabled = !el.parentElement.closest("[data-mural-source]");
    }
    const list = q("[data-editor-layers]"), search = q("[data-editor-search]").value.toLowerCase();
    list.replaceChildren();
    objects().filter(node => label(node).toLowerCase().includes(search)).forEach(node => {
      const button = document.createElement("button"); button.type="button";
      button.className = "page-edit-layer"; button.classList.toggle("on", node === el);
      button.textContent = label(node); button.title = node.tagName.toLowerCase() + (node.id ? ` #${node.id}` : "");
      let depth=0; for (let parent=node.parentElement; parent && parent!==session.root; parent=parent.parentElement) depth++;
      button.style.paddingLeft=`${8+Math.min(depth,5)*12}px`;
      button.onclick = () => select(node); list.append(button);
    });
    outline();
  }
  function fit() {
    if (!session) return;
    const host = q(".page-edit-stage"), {width,height} = session.capture;
    session.scale = Math.min((host.clientWidth-48)/width,(host.clientHeight-48)/height);
    const scale = Math.max(.02, session.scale); session.scale = scale;
    Object.assign(session.frame.style, {width:`${width}px`,height:`${height}px`,
      transform:`scale(${scale})`,left:`${(host.clientWidth-width*scale)/2}px`,top:`${(host.clientHeight-height*scale)/2}px`});
    outline();
  }

  function bindCanvas() {
    const doc = session.frame.contentDocument;
    session.root = doc.querySelector("[data-mural-page]");
    session.baseline = new Map(objects().map(el=>[el.dataset.muralSource, {el, state:state(el), styles:Object.fromEntries(EDIT_STYLES.map(key=>[key,el.style.getPropertyValue(key)]))}]));
    doc.addEventListener("click", event => event.preventDefault(), true);
    doc.addEventListener("pointerdown", event => {
      if (event.button !== 0 || session.busy) return;
      let el = (event.target.closest("svg") || event.target).closest("[data-mural-source]");
      if (event.altKey) el = el?.parentElement.closest("[data-mural-source]") || el;
      if (!el) { select(null); return; }
      if (!session.root.contains(el) || el.matches(NON_OBJECT)) return;
      event.preventDefault(); select(el);
      if (!session.selected) return;
      session.drag = {el, before:state(el), clientX:event.clientX,clientY:event.clientY,
        x:Number(el.dataset.muralX||0),y:Number(el.dataset.muralY||0),r:Number(el.dataset.muralR||0),moved:false};
      el.setPointerCapture(event.pointerId);
    }, true);
    doc.addEventListener("pointermove", event => {
      const drag = session.drag; if (!drag) return;
      const dx = event.clientX-drag.clientX, dy = event.clientY-drag.clientY;
      if (!drag.moved && Math.abs(dx)+Math.abs(dy) < 2) return;
      drag.moved = true; move(drag.el,drag.x+dx,drag.y+dy,drag.r); outline();
    });
    const end = () => {
      const drag = session.drag; session.drag = null;
      if (!drag?.moved) return;
      const after = state(drag.el);
      remember(() => restore(drag.el,drag.before), () => restore(drag.el,after));
    };
    doc.addEventListener("pointerup", end); doc.addEventListener("pointercancel", end);
    doc.addEventListener("dblclick", event => {
      if (isText(session.selected)) { event.preventDefault(); q("[data-prop=text]").focus(); q("[data-prop=text]").select(); }
    });
    doc.addEventListener("keydown", keyboard, true);
    fit(); refresh();
    message(session.capture.canvasCount ? `已保留 ${session.capture.canvasCount} 个画布图表的当前图像；图表数据不在此编辑。` : "点选并拖动对象；双击文字编辑。遮挡对象可从图层列表选择。");
  }
  function keyboard(event) {
    event.stopPropagation();
    if (event.target.closest("input,textarea,select") || session.busy) return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") { event.preventDefault(); travel(event.shiftKey); }
    else if (["Delete","Backspace"].includes(event.key)) { event.preventDefault(); remove(); }
    else if (event.key === "Escape") { event.preventDefault(); session.selected ? select(null) : close(); }
    else if (session.selected && /^Arrow/.test(event.key)) {
      event.preventDefault();
      const [dx,dy] = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
      const step = event.shiftKey ? 10 : 1;
      change(el => move(el,Number(el.dataset.muralX||0)+dx*step,Number(el.dataset.muralY||0)+dy*step,Number(el.dataset.muralR||0)));
    }
  }

  async function embedResources(doc) {
    const cache = new Map(); let total = 0;
    async function dataURL(raw) {
      if (!raw || raw.startsWith("data:") || raw.startsWith("#")) return raw;
      const url = new URL(raw, location.href);
      if (!["http:","https:","blob:"].includes(url.protocol)) throw new Error("页面包含不能离线打包的资源协议。");
      if (!cache.has(url.href)) cache.set(url.href, (async () => {
        const response = await fetch(url.href, {credentials:url.origin === location.origin ? "same-origin" : "omit", signal:AbortSignal.timeout(20000)});
        if (!response.ok) throw new Error(`资源读取失败（${response.status}），未导出不完整 HTML。`);
        const blob = await response.blob(); total += blob.size;
        if (total > 64*1024*1024) throw new Error("当前页资源超过 64 MB，未导出；请缩小替换图片后重试。");
        return new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload=()=>resolve(reader.result); reader.onerror=reject; reader.readAsDataURL(blob); });
      })());
      return cache.get(url.href);
    }
    async function css(text) {
      const matches = [...text.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/g)];
      for (const match of matches) {
        const raw = (match[1] ?? match[2] ?? match[3]).trim();
        text = text.replace(match[0],`url("${await dataURL(raw)}")`);
      }
      return text;
    }
    for (const style of doc.querySelectorAll("style")) style.textContent = await css(style.textContent);
    for (const node of doc.querySelectorAll("*")) {
      if (node.hasAttribute("style")) node.setAttribute("style", await css(node.getAttribute("style")));
      if (node.tagName === "IMG") node.setAttribute("src", await dataURL(node.getAttribute("src")));
      if (node.namespaceURI === "http://www.w3.org/2000/svg") {
        for (const name of ["href","xlink:href"]) if (node.hasAttribute(name)) node.setAttribute(name,await dataURL(node.getAttribute(name)));
      }
      for (const name of ["data-mural-x","data-mural-y","data-mural-r","data-mural-page"]) node.removeAttribute(name);
    }
  }
  async function download() {
    if (!session?.root || session.busy) return;
    flushInput();
    const current = session; current.busy = true; q("[data-editor-export]").disabled = true;
    q(".page-edit-sidebar").inert=true;
    message("正在把当前页图片与字体打包进 HTML…");
    try {
      const doc = new DOMParser().parseFromString(current.frame.contentDocument.documentElement.outerHTML,"text/html");
      await embedResources(doc);
      const blob = new Blob(["<!doctype html>\n"+doc.documentElement.outerHTML],{type:"text/html;charset=utf-8"});
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href=url; link.download=`deck-${current.deckId}-page-${current.page}-edited.html`;
      document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
      message("已下载当前画面副本；尚未保存的修改仍需点击“保存并更新截图”。");
    } catch (error) { message(error.message || "资源打包失败，请稍后重试。"); }
    finally { current.busy=false; if (session === current) { q("[data-editor-export]").disabled=false; q(".page-edit-sidebar").inert=false; refresh(); } }
  }
  function changes() {
    const result=[];
    for (const [id, before] of session.baseline) {
      const {el}=before;
      if (!session.root.contains(el)) {
        const parent=el.parentElement?.closest("[data-mural-source]");
        if (!parent || session.root.contains(parent)) result.push({node:Number(id),delete:true});
        continue;
      }
      const edit={node:Number(id)}, styles={};
      for (const key of EDIT_STYLES) {
        const value=el.style.getPropertyValue(key);
        if (value!==before.styles[key]) styles[key]=value;
      }
      if (Object.keys(styles).length) edit.styles=styles;
      if (isText(el) && before.state.text!==el.innerHTML) edit.text=el.innerText;
      if (el.dataset.muralSourceTag==="img" && before.state.src!==el.getAttribute("src")) edit.src=el.getAttribute("src");
      if (Object.keys(edit).length>1) result.push(edit);
    }
    return result;
  }
  async function save() {
    if (!session?.root || session.busy) return;
    flushInput();
    const updates=changes();
    const current=session;
    current.busy=true; q(".page-edit-sidebar").inert=true;
    q("[data-editor-save]").disabled=true; q("[data-editor-export]").disabled=true;
    message("正在保存原 HTML 并重新渲染当前页，请稍候…");
    try {
      const response=await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${current.deckId}/pages/${current.page}/edit`, {
        method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({version:current.capture.version,changes:updates}), signal:AbortSignal.timeout(250000),
      });
      const result=await response.json();
      if (!response.ok || !result.saved) throw new Error(result.detail || result.error || "页面未保存。");
      current.dirty=false; current.busy=false;
      close();
      await current.onSaved?.(result);
    } catch(error) {
      if (session===current) message(error.message || "保存未确认成功，请重新核对原页面。");
    } finally {
      current.busy=false;
      if (session===current) { q(".page-edit-sidebar").inert=false; q("[data-editor-save]").disabled=false; q("[data-editor-export]").disabled=false; refresh(); }
    }
  }
  function flushInput() { session.pendingField?.dispatchEvent(new Event("change")); }
  function close() {
    if (!session) return true;
    if (session.busy) { message("正在保存或打包，请等当前操作完成。"); return false; }
    flushInput();
    if (session.dirty && !window.confirm("修改尚未保存到原页面。关闭将丢弃本次修改，是否继续？")) return false;
    const old=session; session=null; old.resize.disconnect(); old.dialog.close(); old.dialog.remove(); old.trigger?.focus();
    return true;
  }

  async function open({page, deckId, trigger, onSaved}) {
    if (session) return;
    const snapshot = await loadSource(deckId, Number(page));
    const dialog = document.createElement("dialog"); dialog.className="page-edit-dialog";
    dialog.setAttribute("aria-labelledby","page-edit-title");
    dialog.innerHTML = `<header class="page-edit-header"><div><small>PAGE EDITOR · <span data-editor-changed>原页面</span></small><h2 id="page-edit-title">编辑第 ${Number(page)} 页</h2></div>
      <div class="page-edit-actions"><button type="button" data-editor-undo disabled>撤销</button><button type="button" data-editor-redo disabled>重做</button><button type="button" data-editor-export>下载画面副本</button><button type="button" data-editor-save class="page-edit-primary">保存并更新截图</button><button type="button" data-editor-close aria-label="关闭编辑">关闭</button></div></header>
      <p class="page-edit-note">保存会修改当前页的原 HTML 并更新 PNG，保留修改前版本。原有图表脚本与共享样式保留；修改后的正式导出仍需重新验收。</p>
      <div class="page-edit-body"><div class="page-edit-stage"><iframe title="当前页编辑画布" sandbox="allow-same-origin"></iframe><div class="page-edit-selection" hidden></div></div>
      <aside class="page-edit-sidebar"><p data-editor-empty>点击页面中的文字或图像，或从下方图层列表选择对象。</p><section data-editor-properties hidden><h3 data-editor-name></h3>
        <div class="page-edit-actions"><button type="button" data-editor-parent>选择父级</button><button type="button" data-editor-delete>删除对象</button></div>
        <label data-editor-text>文字<textarea data-prop="text" rows="4"></textarea></label><div class="page-edit-grid">
        ${[["x","位移 X"],["y","位移 Y"],["width","宽度"],["height","高度"],["r","附加旋转 °"],["opacity","不透明度 0–1"],["font-size","字号"]].map(([key,title])=>`<label>${title}<input type="number" data-prop="${key}" step="${key==="opacity"?"0.05":"1"}" ${["width","height","font-size"].includes(key)?'min="1"':key==="opacity"?'min="0" max="1"':""}></label>`).join("")}
        <label>文字颜色<input type="color" data-prop="color"></label></div><label data-editor-replace hidden>替换图片<input type="file" accept="image/png,image/jpeg,image/webp" data-editor-image></label></section>
        <section class="page-edit-layer-section"><h3>页面图层</h3><input type="search" data-editor-search placeholder="查找文字或对象" aria-label="查找图层"><div data-editor-layers></div></section></aside></div>
      <footer><span>${snapshot.width} × ${snapshot.height}</span><span data-editor-status role="status" aria-live="polite">正在准备编辑画布…</span><span>方向键微调 · Shift 加速 · ⌘ / Ctrl Z 撤销</span></footer>`;
    document.body.append(dialog);
    session = {dialog, capture:snapshot, frame:dialog.querySelector("iframe"),deckId,page,trigger,onSaved,selected:null,
      history:[],future:[],transforms:new WeakMap(),dirty:false,busy:false,scale:1};
    dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
    dialog.addEventListener("keydown", keyboard);
    q("[data-editor-close]").onclick=close; q("[data-editor-export]").onclick=download; q("[data-editor-save]").onclick=save;
    q("[data-editor-undo]").onclick=()=>travel(); q("[data-editor-redo]").onclick=()=>travel(true);
    q("[data-editor-parent]").onclick=()=>select(session.selected?.parentElement.closest("[data-mural-source]")); q("[data-editor-delete]").onclick=remove;
    q("[data-editor-search]").oninput=refresh;
    dialog.querySelectorAll("[data-prop]").forEach(input => input.addEventListener("input", () => { session.pendingField=input; }));
    dialog.querySelectorAll("[data-prop]").forEach(input => input.addEventListener("change", () => {
      if (session.busy) return;
      if (session.pendingField===input) session.pendingField=null;
      const key = input.dataset.prop, value=input.value;
      if (!["text","color"].includes(key) && (value.trim()==="" || !Number.isFinite(Number(value)))) { refresh(); return; }
      change(el => {
        if (["x","y","r"].includes(key)) {
          const values = Object.fromEntries(["x","y","r"].map(k=>[k,Number(el.dataset[`mural${k.toUpperCase()}`]||0)]));
          values[key]=Number(value); move(el,values.x,values.y,values.r);
        } else if (key==="text" && isText(el)) { el.textContent=value; el.style.whiteSpace="pre-wrap"; }
        else if (key==="color" && isText(el)) { el.style.setProperty("color",value,"important"); el.style.setProperty("-webkit-text-fill-color",value,"important"); }
        else if (key==="opacity") el.style.setProperty(key,String(Math.max(0,Math.min(1,Number(value)))),"important");
        else if (["width","height","font-size"].includes(key)) el.style.setProperty(key,`${Math.max(1,Number(value))}px`,"important");
      });
    }));
    q("[data-editor-image]").onchange=async event => {
      const current=session, el=current.selected, file=event.target.files?.[0]; event.target.value="";
      if (!el || el.dataset.muralSourceTag!=="img" || !file || current.busy) return;
      if (!["image/png","image/jpeg","image/webp"].includes(file.type) || file.size>12*1024*1024) { message("请选择不超过 12 MB 的 PNG、JPEG 或 WebP。"); return; }
      try {
        const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file);});
        const probe=new Image(); probe.src=data; await probe.decode();
        if (session!==current || !el.isConnected || current.busy) return;
        const before=state(el); el.src=data; const after=state(el);
        remember(()=>restore(el,before),()=>restore(el,after)); message("已替换编辑画布中的图片，确认比例和裁切后保存。");
      } catch { if (session===current) message("图片无法解码，未替换原图。"); }
    };
    const current=session;
    session.frame.addEventListener("load", () => { if (session===current) bindCanvas(); }, {once:true});
    session.resize=new ResizeObserver(fit); session.resize.observe(q(".page-edit-stage"));
    dialog.showModal(); session.frame.srcdoc=snapshot.html;
  }
  window.addEventListener("beforeunload", event => { if (session?.dirty || session?.pendingField) { event.preventDefault(); event.returnValue=""; } });
  window.MuralPageEditor = {open, close};
})();
