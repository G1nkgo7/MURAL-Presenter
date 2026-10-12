// pptagent_static_web_demo — “舞台暗房”工作台:创作简报 → PowerPoint 式编辑器(胶片条 + 画布,实时显影)。
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const pad2 = (n) => String(n).padStart(2, "0");

const STATUS_LABEL = {
  waiting: "等待上一版", queued: "排队中", running: "生成中", completed: "已完成", failed: "失败", rejected: "未通过",
  error: "失败", stopped: "已中断", interrupted: "已中断", max_turns: "已达轮次上限", idle: "待继续",
  paused_recoverable: "通道中断 · 可继续",
  not_started: "未开始",
};
const isStaticActiveStatus = (status) => ["waiting", "queued", "running"].includes(status);
const isStaticGeneratingStatus = (status) => ["queued", "running"].includes(status);
const WORKFLOW_TRACE_REFRESH_MS = 12000;
const WORKFLOW_TRACE_REQUEST_TIMEOUT_MS = 15000;
const PHASE_LABEL = {
  starting: "启动中", planning: "规划大纲", researching: "检索资料", designing: "配图设计",
  delegating: "页面制作", rendering: "页面制作", verifying: "成稿校验", done: "完成", failed: "失败",
  stopped: "已中断", not_started: "未开始",
};
const TOOL_LABEL = {
  read: "研读资料", write: "撰写页面", edit: "修改页面", bash: "渲染画面",
  read_file: "研读资料", write_file: "撰写页面", edit_file: "修改页面",
  patch: "调整页面", terminal: "运行制作工具",
  vision_analyze: "检查画面", web_search: "搜索资料", web_fetch: "查阅网页", web_extract: "提取网页要点",
  image_generate: "绘制配图", fetch_image: "下载图片", delegate_task: "分派任务",
};
function actText(p) {
  const a = p && p.activity;
  if (!a) return "";
  const key = canonicalOrchestrationAgentKey(a.agent);
  const slide = key.match(/^slide_0?(\d+)/);
  let who = "页面制作";
  if (key === "orch") who = "整体策划";
  else if (key.startsWith("research")) who = "资料研究";
  else if (key.startsWith("material")) who = "素材整理";
  else if (key.startsWith("image")) who = "视觉素材";
  else if (key.startsWith("review")) who = "整稿校验";
  else if (key.startsWith("slide_group_")) who = orchestrationAgentMeta(key).label;
  else if (slide) who = `第 ${Number(slide[1])} 页制作`;
  const action = {
    read: "整理资料", read_file: "整理资料", write: "设计页面", write_file: "设计页面",
    edit: "优化页面", edit_file: "优化页面", patch: "优化页面", terminal: "生成页面预览",
    bash: "生成页面预览", vision_analyze: "检查页面效果", image_generate: "生成视觉素材", fetch_image: "下载图片",
    web_search: "查找参考资料", web_fetch: "整理参考资料", web_extract: "提取网页要点", delegate_task: "安排页面制作",
  }[a.tool] || TOOL_LABEL[a.tool] || "推进制作";
  return `${who} · ${action}`;
}
// phase -> stepper key;STEP_ORDER 用于把之前的步标记为已完成
const PHASE_STEP = {
  starting: "plan", planning: "plan", researching: "research", designing: "research",
  delegating: "render", rendering: "render", verifying: "verify", done: "done", failed: null,
};
const STEP_ORDER = ["plan", "research", "render", "verify", "done"];

function runErrorText(value, kind) {
  const message = String(value || "生成失败");
  if (kind === "release_hash_mismatch" || /frozen MURAL release hash mismatch|verify_runtime_release/i.test(message)) {
    return "运行时 Release 校验未通过：当前 skill/harness 与已冻结发布的哈希不一致，任务在首次模型请求前即被拒绝（release_hash_mismatch，非 Provider/API 故障）。";
  }
  if (message.includes("empty_giveup")) {
    return "模型连续返回空响应，自愈重试后仍未恢复。请重新发起任务（empty_giveup）。";
  }
  return message;
}

function ensureStudioNoticeStack() {
  let stack = $("#studio-notice-stack");
  if (stack) return stack;
  stack = document.createElement("div");
  stack.id = "studio-notice-stack";
  stack.className = "studio-notice-stack";
  stack.setAttribute("aria-live", "polite");
  stack.setAttribute("aria-atomic", "false");
  document.body.append(stack);
  return stack;
}

function dismissStudioNotice(notice) {
  if (!notice || notice.classList.contains("leaving")) return;
  clearTimeout(notice.__dismissTimer);
  notice.classList.add("leaving");
  setTimeout(() => notice.remove(), 220);
}

function showStudioNotice(message, { type = "error", duration = 7000 } = {}) {
  const text = String(message || "").trim();
  if (!text) return;
  const stack = ensureStudioNoticeStack();
  const duplicate = [...stack.children].find((item) => item.dataset.message === text && !item.classList.contains("leaving"));
  if (duplicate) {
    duplicate.classList.remove("notice-bump");
    requestAnimationFrame(() => duplicate.classList.add("notice-bump"));
    return duplicate;
  }
  const notice = document.createElement("div");
  notice.className = `studio-notice ${type}`;
  notice.dataset.message = text;
  notice.setAttribute("role", type === "error" ? "alert" : "status");
  notice.innerHTML = `<span class="studio-notice-mark" aria-hidden="true"></span><span class="studio-notice-copy">${escapeHtml(text)}</span><button type="button" aria-label="关闭通知">×</button>`;
  notice.querySelector("button").onclick = () => dismissStudioNotice(notice);
  stack.prepend(notice);
  requestAnimationFrame(() => notice.classList.add("shown"));
  if (duration > 0) notice.__dismissTimer = setTimeout(() => dismissStudioNotice(notice), duration);
  return notice;
}

function clearStudioNotices() {
  const stack = $("#studio-notice-stack");
  if (!stack) return;
  [...stack.children].forEach((notice) => dismissStudioNotice(notice));
}

function notifyRunFailure(value, fallback = "生成未能完成", kind) {
  const message = runErrorText(value || fallback, kind);
  const cancelled = /已取消|取消|已中断|中断|已停止|停止|cancel(?:led|ed)?/i.test(message);
  showStudioNotice(cancelled ? "生成已中断" : message, {
    type: cancelled ? "info" : "error",
    duration: cancelled ? 2800 : 9000,
  });
}

const ed = {                 // 编辑器状态
  id: null, kind: "static", sse: null, timer: null,
  total: 0, authored: new Set(), rendered: new Set(), sel: null, follow: true,
  status: null, startedElapsed: null, elapsedBase: 0, elapsedAt: 0,
  agentTimings: {}, overallTiming: null,
  fullQuery: "",
};
let isAuthenticated = document.querySelector(".layout")?.dataset.authenticated === "1";
const dynamicEnabled = document.querySelector(".layout")?.dataset.dynamicEnabled !== "0";
const trajectoryMode = document.querySelector(".layout")?.dataset.trajectoryMode === "1";
const trajectoryDeckApi = (id, suffix = "") => `${globalThis.STUDIO_BASE_PATH || ""}/api/trajectory-monitor/decks/${encodeURIComponent(id)}${suffix}`;
let resumeGenerationAfterAuth = false;
let composerLaunchMorph = null;
let pageViewTransitionToken = 0;
let pageViewTransitionTarget = null;
const taskComposerDrafts = new Map();

function taskComposerKey(kind = ed.kind, id = ed.id) {
  return `${kind || "static"}:${id || "new"}`;
}

function beginComposerLaunch(query) {
  // Query 会在新工作区中直接成为右侧消息。这里只保存一次轻量转场状态，
  // 不再复制并放大输入框，避免长 Query 形成遮挡页面的悬浮层。
  composerLaunchMorph = { query: String(query || "").trim() };
  $("#composer")?.classList.add("is-sending-query");
  const send = $("#send");
  if (send) {
    send.dataset.idleLabel ||= send.textContent;
    send.textContent = creationMode === "dynamic" ? "正在启动动态演示…" : "正在启动静态演示…";
    send.setAttribute("aria-busy", "true");
  }
}

function clearComposerLaunchMorph({ restore = false } = {}) {
  composerLaunchMorph = null;
  $("#composer")?.classList.remove("is-sending-query");
  const send = $("#send");
  if (send) {
    send.textContent = send.dataset.idleLabel || uiText("generate");
    send.removeAttribute("aria-busy");
  }
}

function finishComposerLaunchMorph() {
  const query = $("#outline-user-query")?.closest(".outline-user-turn");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  clearComposerLaunchMorph();
  if (reduced || typeof query?.animate !== "function") return;
  query.animate(
    [{ opacity: 0, transform: "translate3d(18px,10px,0) scale(.985)" }, { opacity: 1, transform: "none" }],
    { duration: 460, delay: 80, easing: "cubic-bezier(.16,1,.3,1)", fill: "backwards" },
  );
}

async function transitionComposerToEditor() {
  const main = $("#main");
  const composer = $("#composer");
  const editor = $("#editor");
  if (!main || !composer || !editor) return;
  if (composer.hidden || !composerLaunchMorph || matchMedia("(prefers-reduced-motion: reduce)").matches
      || typeof composer.animate !== "function") {
    composer.hidden = true;
    editor.hidden = false;
    return;
  }

  main.classList.add("workspace-launch-transition");
  composer.classList.add("workspace-launch-source");
  editor.classList.add("workspace-launch-target");
  editor.hidden = false;

  const sourceAnimation = composer.animate([
    { opacity: 1, transform: "translate3d(0,0,0) scale(1)", filter: "blur(0)" },
    { opacity: .72, offset: .38 },
    { opacity: 0, transform: "translate3d(0,-10px,0) scale(.992)", filter: "blur(2px)" },
  ], { duration: 430, easing: "cubic-bezier(.4,0,.2,1)", fill: "both" });
  const targetAnimation = editor.animate([
    { opacity: 0, transform: "translate3d(0,10px,0) scale(.994)" },
    { opacity: 0, offset: .16 },
    { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
  ], { duration: 500, easing: "cubic-bezier(.16,1,.3,1)", fill: "both" });

  await Promise.allSettled([sourceAnimation.finished, targetAnimation.finished]);
  composer.hidden = true;
  sourceAnimation.cancel();
  targetAnimation.cancel();
  composer.classList.remove("workspace-launch-source");
  editor.classList.remove("workspace-launch-target");
  main.classList.remove("workspace-launch-transition");
}

function refreshDeckListWhenIdle(active) {
  const refresh = () => loadDecks(active);
  if (typeof requestIdleCallback === "function") requestIdleCallback(refresh, { timeout: 900 });
  else setTimeout(refresh, 80);
}

const SUGGESTION_POOL = [
  {
    label: "城市户外品牌 · 中庭快闪提案",
    query: `下周我们要去跟商场的招商总监谈一个核心中庭的快闪店位置。我们是一个全新的城市户外品牌，要让商场相信我们能吸引最高净值的年轻客流。请帮我做一份20页左右的入驻提案PPT。
第一部分不讲常规的品牌故事，直接放满墙的穿搭图，讲山系机能风怎么成了现在一线城市新中产的标配。
第二部分讲我们的空间策展概念。不要普通货架，我们要在商场中庭搭一个带溪流的室内原始森林，并把空间效果图放上去。
第三部分讲客群和社群运营。我们不仅卖冲锋衣，周末还会组织飞盘、露营和溯溪，重点强调超高复购率和高客单价。最后给商场展示商业回报，测算一个月快闪能为商场带来的全网曝光量、小红书打卡数和预估GMV。
排版采用最前沿的山系机能潮流风，抛弃传统PPT的规矩感。底色用岩石灰或水泥灰，搭配极具视觉冲击力的荧光橙或荧光绿作为点缀。图片要混搭：一半是粗犷的高清雪山、森林摄影，一半是极具工业感的反光面料微距图。字体使用极其粗犷的黑体，字号要特别大，像街头潮流海报的大字报排版。整体要有强烈的野性质感。`,
  },
  {
    label: "山系度假民宿 · 招商与 OTA",
    query: `我新开了一家山系度假民宿，需要一份25页左右的产品介绍资料PPT，用于招商洽谈和OTA平台上架资料参考。
内容上包括民宿招商该有的板块：选址与环境卖点、房型配置与定价策略、配套设施与体验活动、目标客群画像、淡旺季运营思路、合规资质与安全保障，其他你判断该补的也一并补齐。
配色定一个主色搭配一个辅助色，字体走山系度假那种松弛、自然、有呼吸感的调性。整体要精致、有设计感，排版要留得开、透得出山野气息，可以适度用大图叙事、房型信息卡片、简洁的数据图或时间线来讲清楚运营节奏。`,
  },
  {
    label: "《沙丘》· 电影美学拆解",
    query: `下周我要给影迷社群做一次《沙丘》电影美学与世界观设定的视觉拆解分享，25到35页之间，页数你根据内容节奏定。
内容层面希望做扎实，别停留在剧情复述。围绕几个维度展开：厄拉科斯星球的地理与生态设定、香料经济与政治权力结构、弗雷曼人的文化与宗教符号、各大家族的视觉识别体系（服装、纹章、建筑语言）、导演维伦纽瓦和摄影师 Greig Fraser 的镜头语言与构图逻辑、汉斯·季默配乐与声音设计的美学取向、以及影片对粗野主义建筑和中东—北非文化的借鉴。每一块都要给出可以拿来跟朋友聊的具体观察和例证，涉及的设定、幕后信息、参考来源请核实准确。
视觉做成一本顶级好莱坞概念艺术图册的质感，追求巨物压迫感和粗野主义的沉重体量。整体色板用废土黄、香料橘、沙岩灰。大量使用宽画幅满版剧照或概念图，让画面自己说话。字体选硬朗、字重偏轻的现代无衬线，营造干燥、灼热的空气感。页面节奏要有电影分镜的呼吸——极空的留白页、纯黑过场页和铺满巨物的震撼页交替出现。`,
  },
  {
    label: "乡村振兴 · 产业路线汇报",
    query: `我们驻村工作队下周要给镇里做乡村振兴项目进展汇报，报告体量控制在25页左右。整体叙事要先压住情绪再把希望托起来：前面讲村子现状和困境要平实记录，用真实数据慢慢加压；第15页左右的转折页必须做出极强的视觉冲击，色调瞬间从深沉切到暖橘色带来希望感；结尾落点要靠事实升华。
中间核心是四条产业路线（果蔬、民宿、电商、加工）的深度横评。请按“投入、带动户数、周期、风险、可持续”五个统一维度，设计一页极具记忆点的 4×5 打分矩阵（使用高对比度热力色块或多边形雷达图组合，严禁默认表格），并据此明确给出要押注哪条路线的量化理由。请自行调研核实真实的行业参考数据。
视觉美术走“大地纪实与破晓旭日”的杂志风。前半段使用深炭黑、泥土褐与粗糙夯土肌理，转折后引入大面积破晓暖光与自然图像；封面、转场及核心页对标《国家地理》的排版质感，用大画幅真实乡村影像结合不规则网格，拒绝同一版式反复套用。`,
  },
  {
    label: "山区山洪 · 应急预案",
    query: `下周要去给文旅局汇报一份山区农家乐的山洪应急预案，帮我出一份22页左右的对比分析方案。核心是详细拆解“沿河预警广播”与“手机短信推送”两种预警方式的实战优劣。
第一部分调研并引用真实可查的山洪气象灾害数据；第二部分对比两种方案的响应延迟、信号死角覆盖率和断网断电极端天气下的可靠性；第三部分给出一套针对农家乐店主和游客的极简疏散 SOP。结论必须是可直接落地的实操建议。
视觉采用“生态地貌与数字预警”融合风：森林苔藓绿、岩石灰配应急霓虹橙，贯穿地形等高线与气象雷达热力图，用半透明毛玻璃科技 UI 承载信息。核心页设计成上帝视角的 3D 微缩山水地貌模型；两种预警的对比使用多维雷达图和强反差左右分屏，避免默认表格和柱状图。`,
  },
  {
    label: "开源软件 · 像素 RPG 科普",
    query: `马上要在高校社团给大学生做一场关于开源软件的科普讲座，制作一份25页左右的演示文稿，内容必须实用且接地气。
第一部分讲透什么是开源，并核查、引用学生每天都在使用却不知道是开源的真实软件案例；第二部分重点拆解 MIT、Apache、GPL 等常见许可证的核心区别，结合真实的开源维权或商业冲突案例，给出新手避坑指南，说明哪些能自由使用、哪些使用后必须公开自己的代码。
视觉使用“8-bit 复古像素 RPG 游戏”风格，设置一个像素极客 NPC 作为向导，背景采用街机深渊黑、CRT 扫描线和粗颗粒像素网格，配色用高亮青、电光黄和品红。封面与章节页做成游戏启动界面和关卡地图；许可证区别做成 RPG 技能树或阵营九宫格，信息框统一为复古游戏对话框。`,
  },
  {
    label: "农业卫星 · 太空老农历",
    query: `我要给省农业厅和农业合作社农户代表做一份15页的农业监测低轨卫星星座组网发射科普与宣介PPT。为了让农户和基层工作者感受到技术价值，整体采用温馨质朴的现代手绘绘本风，把高精尖航天科技化作亲切的“天上新农具”，主色为阳光暖黄、沃野泥土棕和嫩芽初绿。
内容按太空老农历“春耕、夏长、秋收、冬藏”推进：星座组网总览；卫星载荷与光谱成像原理；火箭发射与入轨步骤；终端数据接收；经济效益与助农政策落地。用太空稻草人、农作物体检报告、太空医生听诊器和智能老农历手机界面等比喻讲清复杂技术。
排版大量使用手绘麦穗、小拖拉机和笑脸云朵点缀，进度条做成种子发芽到结出硕果的插画演变，让硬核航天项目充满接地气的人情味。`,
  },
  {
    label: "家庭收纳 · 小白实用课程",
    query: `想做一套家庭收纳整理的实用课程，面向完全没经验的小白，控制在24页左右。重点讲衣柜、厨房和玄关三个常见场景，用真实家庭案例展示整理前后对比和具体操作步骤。整体用轻松明快的卡通插画风，排版干净有设计感，让人看完就能照着动手整理。`,
  },
  {
    label: "《霸王龙的晚餐》· 睡前科普",
    query: `我想制作一份20页左右、面向4—8岁小朋友的睡前交互式科普课件PPT，主题叫《霸王龙的晚餐》。不要任何血腥撕咬或吓人的画面，要温馨幽默、充满童趣：以一只肚子咕咕叫但很友善的小霸王龙寻找晚饭为故事主线，带领小朋友认识不同恐龙的身体特征与生活环境。
内容包括小霸王龙登场与恐龙世界地图、猜脚印和小短手互动；逐页探索梁龙、甲龙、三角龙与副栉龙；展示白垩纪密林、火山湖畔与蕨类植物森林；结尾反转为恐龙伙伴们举办树叶与野果晚宴，并自然引导小朋友准备睡觉。每页配大字号恐龙名片与极简互动知识。
视觉采用温馨治愈的暖心绘本插画风，以燕麦奶黄和天空蓝为底，搭配暖阳黄、薄荷绿、珊瑚粉与小橘红。每页都有圆滚滚大眼睛的拟人化恐龙，字体使用超大字号软萌字体，版式采用翻翻书、卡通对话框与手绘点缀，让整套课件像一本温馨睡前童话。`,
  },
];

const NOVA_DOT_FONT = {
  A: ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
  E: ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
  L: ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
  M: ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
  N: ["10001", "11001", "11001", "10101", "10011", "10011", "10001"],
  O: ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
  P: ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
  R: ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
  S: ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
  T: ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
  U: ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
  V: ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
};

function initNovaDoodle() {
  const host = $("#nova-doodle");
  const canvas = $("#nova-doodle-canvas");
  const context = canvas?.getContext("2d");
  if (!host || !canvas || !context) return;

  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const dots = [];
  const brandDots = [];
  const brandLetters = "MURAL";
  const letters = "PRESENT";
  const centers = [53, 102, 151, 200, 249, 298, 347];
  const brandCenters = [102, 151, 200, 249, 298];
  const brandCenterY = 78;
  const accentMap = [
    { row: 2, col: 1, color: "blue" }, null,
    { row: 6, col: 3, color: "purple" }, null,
    { row: 5, col: 4, color: "mint" }, null,
    { row: 3, col: 4, color: "coral" },
  ];
  const switchLogo = host.querySelector(".nova-switch-logo");
  let visible = true;
  let frame = 0;
  let startedAt = performance.now();
  let pointerX = .5;
  let pointerY = .5;
  let logoActive = false;

  function abstractStrength(tile, row, col, letterOn) {
    const dx = col - 3;
    const dy = row - 4;
    const radius = Math.hypot(dx * 1.08, dy);
    const orbit = Math.max(0, 1 - Math.abs(radius - (2.2 + (tile % 3) * .24)) / .68);
    const wave = Math.max(0, 1 - Math.abs(dx - Math.sin((dy + tile) * .75) * .64) / .78);
    // The word never disappears: only its surrounding field reorganizes.
    return Math.max(letterOn ? .78 : .11, (tile % 2 ? orbit : wave) * (letterOn ? .42 : .58));
  }

  centers.forEach((centerX, tile) => {
    const glyph = NOVA_DOT_FONT[letters[tile]];
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 7; col += 1) {
        const dx = col - 3;
        const dy = row - 4;
        // 七个紧凑的圆角粒子场在同一帧中完整呈现 PRESENT。
        if (Math.hypot(dx * 1.14, dy) > 4.72) continue;
        const glyphRow = row - 1;
        const glyphCol = col - 1;
        const letterOn = glyphRow >= 0 && glyphRow < 7 && glyphCol >= 0 && glyphCol < 5
          && glyph[glyphRow][glyphCol] === "1";
        const accent = accentMap[tile];
        dots.push({
          tile, row, col,
          x: centerX + dx * 5.35,
          y: 78 + dy * 6.15,
          letterStrength: letterOn ? 1 : .10 + ((row * 13 + col * 7 + tile * 5) % 5) * .045,
          abstractStrength: abstractStrength(tile, row, col, letterOn),
          accent: accent && row === accent.row && col === accent.col ? accent.color : "",
          seed: (tile * 97 + row * 19 + col * 31) % 113,
          delay: tile * 62 + Math.hypot(dx, dy) * 28 + ((row + col) % 3) * 20,
        });
      }
    }
  });

  brandCenters.forEach((centerX, tile) => {
    const glyph = NOVA_DOT_FONT[brandLetters[tile]];
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 7; col += 1) {
        const dx = col - 3;
        const dy = row - 4;
        if (Math.hypot(dx * 1.14, dy) > 4.72) continue;
        const glyphRow = row - 1;
        const glyphCol = col - 1;
        const letterOn = glyphRow >= 0 && glyphRow < 7 && glyphCol >= 0 && glyphCol < 5
          && glyph[glyphRow][glyphCol] === "1";
        const accent = accentMap[tile];
        brandDots.push({
          tile, row, col,
          x: centerX + dx * 5.35,
          y: brandCenterY + dy * 6.15,
          letterStrength: letterOn ? 1 : .10 + ((row * 13 + col * 7 + tile * 5) % 5) * .045,
          abstractStrength: abstractStrength(tile, row, col, letterOn),
          accent: accent && row === accent.row && col === accent.col ? accent.color : "",
          seed: (tile * 97 + row * 19 + col * 31) % 113,
          delay: tile * 62 + Math.hypot(dx, dy) * 28 + ((row + col) % 3) * 20,
        });
      }
    }
  });

  function palette() {
    return document.documentElement.dataset.theme === "dark"
      ? {
        base: [61, 62, 67], mid: [123, 124, 132], bright: [229, 229, 234],
        blue: [130, 205, 255], purple: [229, 125, 224], mint: [128, 223, 135], coral: [255, 141, 147],
        yellow: [242, 211, 88], lime: [116, 229, 126],
        brandPurple: [179, 145, 255], brandGreen: [29, 226, 171],
      }
      : {
        base: [211, 209, 217], mid: [134, 130, 143], bright: [64, 60, 72],
        blue: [53, 154, 226], purple: [184, 53, 173], mint: [35, 167, 92], coral: [225, 77, 88],
        yellow: [196, 153, 25], lime: [31, 166, 87],
        brandPurple: [100, 55, 220], brandGreen: [0, 184, 132],
      };
  }

  function colorMix(a, b, amount, alpha = 1) {
    const mix = a.map((value, index) => Math.round(value + (b[index] - value) * amount));
    return `rgba(${mix[0]},${mix[1]},${mix[2]},${alpha})`;
  }

  function brandPointColor(colors, position, alpha = 1) {
    // MURAL 仅替换颜色，点阵几何与动画时序仍和 PRESENT 完全共用。
    const tone = brandPointTone(colors, position);
    return colorMix(tone, tone, 1, alpha);
  }

  function brandPointTone(colors, position) {
    const blend = easeState(Math.max(0, Math.min(1, (position - .30) / .58)));
    return colors.brandPurple.map((value, index) => Math.round(
      value + (colors.brandGreen[index] - value) * blend
    ));
  }

  function easeState(value) {
    const t = Math.max(0, Math.min(1, value));
    return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function abstractAmount(elapsed) {
    if (motion.matches) return 0;
    const cycle = elapsed % 10400;
    if (cycle < 3300) return 0;
    if (cycle < 4500) return easeState((cycle - 3300) / 1200);
    if (cycle < 6500) return 1;
    if (cycle < 7700) return 1 - easeState((cycle - 6500) / 1200);
    return 0;
  }

  function brandState(elapsed) {
    if (motion.matches) return { brand: 0, active: false, direction: 0, progress: 0 };
    const cycle = elapsed % 11200;
    if (cycle < 3900) return { brand: 0, active: false, direction: 0, progress: 0 };
    if (cycle < 5200) {
      const progress = (cycle - 3900) / 1300;
      return { brand: easeState(progress), active: true, direction: 1, progress };
    }
    if (cycle < 7600) return { brand: 1, active: false, direction: 0, progress: 0 };
    if (cycle < 8900) {
      const progress = (cycle - 7600) / 1300;
      return { brand: 1 - easeState(progress), active: true, direction: -1, progress };
    }
    return { brand: 0, active: false, direction: 0, progress: 0 };
  }

  function syncLogoState(transition) {
    if (!switchLogo) return;
    const shouldShow = transition.brand > .015;
    host.classList.toggle("brand-visible", shouldShow);
    if (shouldShow && !logoActive) {
      switchLogo.classList.remove("logo-redraw");
      void switchLogo.getBoundingClientRect();
      switchLogo.classList.add("logo-redraw");
    }
    logoActive = shouldShow;
  }

  function brandAmount(elapsed) {
    return brandState(elapsed).brand;
  }

  function dotSwitchState(dot, transition, isBrand) {
    if (!transition.active) {
      return {
        alpha: isBrand ? transition.brand : 1 - transition.brand,
        scatter: 0,
        burst: 0,
        entering: false,
      };
    }
    const entering = transition.direction > 0 ? isBrand : !isBrand;
    const jitter = ((dot.seed * 37 + dot.tile * 17 + dot.row * 11 + dot.col * 7) % 101) / 100;
    const local = entering
      ? Math.max(0, Math.min(1, (transition.progress - .12 - jitter * .22) / .66))
      : Math.max(0, Math.min(1, (transition.progress - jitter * .20) / .68));
    const eased = easeState(local);
    return {
      alpha: entering ? eased : 1 - eased,
      scatter: entering ? 1 - eased : eased,
      burst: Math.sin(Math.PI * local),
      entering,
    };
  }

  function scatterVector(dot, state, direction) {
    const angle = dot.seed * .31 + dot.tile * 1.47 + dot.row * .22 - dot.col * .19;
    const spin = state.burst * (state.entering ? -1 : 1) * direction * 1.08;
    const distance = (16 + (dot.seed % 19) * 1.65) * state.scatter;
    return {
      x: Math.cos(angle + spin) * distance + Math.sin(dot.row + dot.seed) * state.burst * 5.5,
      y: Math.sin(angle + spin) * distance * .64 + Math.cos(dot.col * .8 + dot.seed) * state.burst * 4.5,
    };
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(devicePixelRatio || 1, 2.5);
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    draw(performance.now());
  }

  function draw(now) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const sx = rect.width / 400;
    const sy = rect.height / 154;
    const dpr = canvas.width / rect.width;
    const colors = palette();
    const elapsed = now - startedAt;
    const abstract = abstractAmount(elapsed);
    const transition = brandState(elapsed);
    syncLogoState(transition);
    const transitionEnergy = transition.active ? Math.sin(Math.PI * transition.progress) : 0;
    const sweep = ((elapsed / 4300) % 1) * 380 + 10;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    context.save();
    for (const dot of dots) {
      const switchState = dotSwitchState(dot, transition, false);
      if (switchState.alpha <= .002) continue;
      context.globalAlpha = switchState.alpha;
      const reveal = motion.matches ? 1 : Math.max(0, Math.min(1, (elapsed - dot.delay) / 460));
      if (!reveal) continue;
      const revealEase = 1 - Math.pow(1 - reveal, 3);
      const localDelay = motion.matches ? 0 : Math.max(0, Math.min(1, abstract * 1.48 - dot.tile * .028 - dot.seed * .0008));
      const state = easeState(localDelay);
      const strength = dot.letterStrength + (dot.abstractStrength - dot.letterStrength) * state;
      const shimmer = motion.matches ? 0 : Math.max(0, 1 - Math.abs(dot.x - sweep) / 25) * .26;
      const pointerDistance = Math.hypot(dot.x / 400 - pointerX, dot.y / 154 - pointerY);
      const pointerPower = motion.matches ? 0 : Math.max(0, 1 - pointerDistance / .16);
      const breath = motion.matches ? 0 : Math.sin(elapsed * .0019 + dot.seed * .19 + dot.tile) * .06;
      const orbit = transitionEnergy * Math.sin(dot.seed * .33 + dot.tile) * 1.35;
      const scatter = scatterVector(dot, switchState, transition.direction);
      const hoverOffset = pointerPower * 1.55;
      const px = (dot.x + scatter.x + Math.cos(dot.seed) * orbit + (dot.x / 400 - pointerX) * hoverOffset) * sx;
      const py = (dot.y + scatter.y + Math.sin(dot.seed) * orbit + (dot.y / 154 - pointerY) * hoverOffset) * sy;
      const intensity = Math.max(0, Math.min(1, strength + shimmer + pointerPower * .24 + breath));
      const radius = (2.0 + intensity * .56 + breath * .28) * Math.min(sx, sy) * revealEase;
      const accentColor = dot.accent ? colors[dot.accent] : null;
      const fill = accentColor
        ? colorMix(colors.mid, accentColor, .82, .93)
        : colorMix(colors.base, colors.bright, intensity, .84 + intensity * .15);

      if (dot.accent || intensity > .86) {
        context.beginPath();
        context.arc(px, py, radius * (dot.accent ? 2.25 : 1.75), 0, Math.PI * 2);
        context.fillStyle = dot.accent
          ? colorMix(accentColor, accentColor, 1, .12)
          : colorMix(colors.bright, colors.bright, 1, .035);
        context.fill();
      }
      context.beginPath();
      context.arc(px, py, Math.max(.55, radius), 0, Math.PI * 2);
      context.fillStyle = fill;
      context.fill();
    }
    context.restore();

    context.save();
    for (const dot of brandDots) {
      const switchState = dotSwitchState(dot, transition, true);
      if (switchState.alpha <= .002) continue;
      context.globalAlpha = switchState.alpha;
      const reveal = motion.matches ? 1 : Math.max(0, Math.min(1, (elapsed - dot.delay) / 460));
      if (!reveal) continue;
      const revealEase = 1 - Math.pow(1 - reveal, 3);
      const localDelay = motion.matches ? 0 : Math.max(0, Math.min(1, abstract * 1.48 - dot.tile * .028 - dot.seed * .0008));
      const state = easeState(localDelay);
      const strength = dot.letterStrength + (dot.abstractStrength - dot.letterStrength) * state;
      const shimmer = motion.matches ? 0 : Math.max(0, 1 - Math.abs(dot.x - sweep) / 25) * .26;
      const pointerDistance = Math.hypot(dot.x / 400 - pointerX, dot.y / 154 - pointerY);
      const pointerPower = motion.matches ? 0 : Math.max(0, 1 - pointerDistance / .16);
      const breath = motion.matches ? 0 : Math.sin(elapsed * .0019 + dot.seed * .19 + dot.tile) * .06;
      const orbit = transitionEnergy * Math.sin(dot.seed * .33 + dot.tile) * 1.35;
      const scatter = scatterVector(dot, switchState, transition.direction);
      const hoverOffset = pointerPower * 1.55;
      const px = (dot.x + scatter.x + Math.cos(dot.seed) * orbit + (dot.x / 400 - pointerX) * hoverOffset) * sx;
      const py = (dot.y + scatter.y + Math.sin(dot.seed) * orbit + (dot.y / 154 - pointerY) * hoverOffset) * sy;
      const intensity = Math.max(0, Math.min(1, strength + shimmer + pointerPower * .24 + breath));
      const radius = (2.0 + intensity * .56 + breath * .28) * Math.min(sx, sy) * revealEase;
      const gradient = Math.max(0, Math.min(1, (dot.x - 24) / 352));
      const brandTone = brandPointTone(colors, gradient);
      const fill = dot.accent
        ? colorMix(colors.mid, brandTone, .82, .93)
        : colorMix(colors.base, brandTone, intensity, .84 + intensity * .15);

      if (dot.accent || intensity > .86) {
        context.beginPath();
        context.arc(px, py, radius * (dot.accent ? 2.25 : 1.75), 0, Math.PI * 2);
        context.fillStyle = brandPointColor(colors, gradient, dot.accent ? .12 : .035);
        context.fill();
      }
      context.beginPath();
      context.arc(px, py, Math.max(.55, radius), 0, Math.PI * 2);
      context.fillStyle = fill;
      context.fill();
    }
    context.restore();
    host.classList.add("doodle-ready");
  }

  function animate(now) {
    draw(now);
    frame = visible && !motion.matches && !document.hidden ? requestAnimationFrame(animate) : 0;
  }

  function resume() {
    if (visible && !motion.matches && !document.hidden && !frame) frame = requestAnimationFrame(animate);
    else draw(performance.now());
  }

  const observer = new IntersectionObserver((entries) => {
    visible = entries[0]?.isIntersecting !== false;
    if (!visible && frame) { cancelAnimationFrame(frame); frame = 0; }
    resume();
  }, { rootMargin: "80px" });
  observer.observe(host);
  new ResizeObserver(resize).observe(canvas);
  document.addEventListener("visibilitychange", resume);
  motion.addEventListener?.("change", resume);
  new MutationObserver(() => draw(performance.now())).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect();
    pointerX = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    pointerY = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
  });
  canvas.addEventListener("pointerleave", () => { pointerX = .5; pointerY = .5; });
  resize();
  resume();
}

// app.js is deferred, so the DOM is ready here. Draw synchronously to avoid a
// one-frame flash of the legacy fallback logo on first load or hard refresh.
initNovaDoodle();

function renderSuggestions() {
  const list = $("#suggestion-list");
  if (!list) return;
  const picks = [...SUGGESTION_POOL].sort(() => Math.random() - 0.5).slice(0, 4);
  list.innerHTML = picks.map((item, index) =>
    `<button type="button" class="suggestion-chip" style="--chip-index:${index}" data-query="${escapeHtml(item.query)}">${escapeHtml(item.label)}</button>`
  ).join("");
}

const STYLE_LABEL = {
  "": "智慧匹配", "商务正式": "商务正式", "学术严谨": "学术严谨",
  "科技感": "科技感", "活泼明快": "彩色创意", "极简": "极简留白",
};

function syncStylePicker(value = "") {
  const select = $("#style-sel");
  const safeValue = [...select.options].some((option) => option.value === value) ? value : "";
  select.value = safeValue;
  $("#style-current").textContent = STYLE_LABEL[safeValue] || safeValue || "智慧匹配";
  $$(".style-card").forEach((card) => {
    const selected = card.dataset.style === safeValue;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

function setStyleGallery(open) {
  const gallery = $("#style-gallery");
  gallery.hidden = !open;
  $("#style-trigger").classList.toggle("active", open);
  $("#style-trigger").setAttribute("aria-expanded", open ? "true" : "false");
}

const LENGTH_LABEL = { "0": "智慧推荐", "4": "精简", "8": "适中", "14": "详细" };

function syncLengthPicker(value = "0") {
  const select = $("#length-sel");
  const safeValue = [...select.options].some((option) => option.value === String(value)) ? String(value) : "0";
  select.value = safeValue;
  $("#length-current").textContent = LENGTH_LABEL[safeValue] || "智慧推荐";
  $$("#length-menu [data-length]").forEach((item) => {
    const selected = item.dataset.length === safeValue;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function setLengthMenu(open) {
  const menu = $("#length-menu");
  const trigger = $("#length-trigger");
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

function syncDeckLanguagePicker(value = "") {
  $("#deck-language-sel").value = value;
  $("#deck-language-current").textContent = value === "zh" ? "中文" : value === "en" ? "English" : uiText("deck_language_auto_short");
  $$("#deck-language-menu [data-deck-language]").forEach((item) => {
    const selected = item.dataset.deckLanguage === value;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-checked", selected ? "true" : "false");
  });
}

function setDeckLanguageMenu(open) {
  $("#deck-language-menu").hidden = !open;
  $("#deck-language-trigger").classList.toggle("active", open);
  $("#deck-language-trigger").setAttribute("aria-expanded", open ? "true" : "false");
}

function syncModelPicker() {
  const select = $("#model-sel");
  const option = select?.selectedOptions?.[0];
  if (!select) return;
  $("#model-current").textContent = option?.textContent.trim() || "配置模型";
  $("#model-trigger")?.classList.toggle("model-empty", !option);
  $$("#model-menu [data-model]").forEach((item) => {
    const selected = item.dataset.model === select.value;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  syncThinkingControl();
}

function renderModelMenu() {
  const menu = $("#model-menu");
  const select = $("#model-sel");
  if (!menu || !select) return;
  const options = [...select.options];
  menu.innerHTML = `<div class="model-menu-title">模型</div>${options.length ? options.map((option) => {
    const custom = option.dataset.custom === "1";
    return `<div class="model-menu-row"${custom ? ` data-custom-model="1"` : ""}>
      <button type="button" class="model-menu-option" data-model="${escapeHtml(option.value)}"${option.disabled ? " disabled" : ""}>
        <span>${custom ? "✦" : "◇"}</span><strong>${escapeHtml(option.textContent.trim())}</strong><small>${custom ? "自定义" : "部署配置"}</small><i>✓</i>
      </button>
      ${custom ? `<button type="button" class="model-menu-delete" data-delete-model="${escapeHtml(option.value)}" aria-label="删除 ${escapeHtml(option.textContent.trim())}" title="删除模型">×</button>` : ""}
    </div>`;
  }).join("") : `<div class="model-menu-empty">尚未配置模型</div>`}
    <div class="model-menu-separator"></div>
    <button type="button" class="model-menu-manage" data-configure-model>
      <span>＋</span><strong>${options.length ? "管理模型" : "添加模型"}</strong><i>›</i>
    </button>`;
  syncModelPicker();
}

function setModelMenu(open) {
  const menu = $("#model-menu");
  const trigger = $("#model-trigger");
  if (!menu || !trigger) return;
  if (open) renderModelMenu();
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

function syncSkillPicker() {
  const select = $("#skill-sel");
  const current = $("#skill-current");
  const control = $("#skill-control");
  const trigger = $("#skill-trigger");
  if (!select || !current || !control || !trigger) return;
  const dynamic = creationMode === "dynamic";
  control.classList.remove("fixed");
  trigger.setAttribute("aria-disabled", "false");
  current.textContent = dynamic ? "MURAL Dynamic HTML" : (select.selectedOptions[0]?.textContent.trim() || "Auto（自动选择）");
  $$("#skill-menu [data-skill]").forEach((item) => {
    const selected = dynamic ? item.dataset.skill === dynamicSkill : item.dataset.skill === select.value;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function renderSkillMenu() {
  const menu = $("#skill-menu");
  const select = $("#skill-sel");
  if (!menu || !select) return;
  const options = creationMode === "dynamic"
    ? [{ value: "sense-present-dazzle", label: "MURAL Dynamic HTML", disabled: false, title: "sn-ppt-dazzle + 专属动态 Harness" }]
    : [...select.options]
        .filter((option) => option.value !== "long-horizon")
        .map((option) => ({
          value: option.value, label: option.textContent.trim(), disabled: option.disabled, title: option.title || "",
        }));
  menu.innerHTML = `<div class="skill-menu-title">Skill</div>${options.map((option) => `
    <button type="button" data-skill="${escapeHtml(option.value)}"${option.disabled ? " disabled" : ""} title="${escapeHtml(option.title)}">
      <span>✦</span><strong>${escapeHtml(option.label)}</strong><i>✓</i>
    </button>`).join("")}`;
  syncSkillPicker();
}

function setSkillMenu(open) {
  const menu = $("#skill-menu");
  const trigger = $("#skill-trigger");
  if (!menu || !trigger) return;
  const next = !!open;
  if (next) renderSkillMenu();
  menu.hidden = !next;
  trigger.classList.toggle("active", next);
  trigger.setAttribute("aria-expanded", next ? "true" : "false");
}

function setUserMenu(open) {
  const menu = $("#user-menu");
  const trigger = $("#user-menu-trigger");
  if (!menu || !trigger) return;
  if (!open) setFontPanel(false);
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

let authTabTransitionToken = 0;

function setAuthTab(tab = "login", { animate = true, focus = true } = {}) {
  const next = tab === "register" ? "register" : "login";
  const tabs = $(".auth-tabs");
  const stage = $("#auth-form-stage");
  const loginForm = $("#auth-login-form");
  const registerForm = $("#auth-register-form");
  const current = stage?.dataset.active === "register" ? "register" : "login";
  const outgoing = current === "login" ? loginForm : registerForm;
  const incoming = next === "login" ? loginForm : registerForm;
  const focusInput = () => {
    if (!focus) return;
    requestAnimationFrame(() => $(next === "login" ? "#auth-login-username" : '#auth-register-form input[name="username"]')?.focus());
  };

  if (tabs) tabs.dataset.active = next;
  $$("[data-auth-tab]").forEach((button) => {
    const selected = button.dataset.authTab === next;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-selected", selected ? "true" : "false");
  });
  const gateError = $("#auth-gate-error");
  if (gateError) gateError.hidden = true;

  if (!stage || !loginForm || !registerForm || current === next) {
    if (loginForm) loginForm.hidden = next !== "login";
    if (registerForm) registerForm.hidden = next !== "register";
    if (stage) stage.dataset.active = next;
    focusInput();
    return;
  }

  const token = ++authTabTransitionToken;
  const reducedMotion = !animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  stage.getAnimations().forEach((animation) => animation.cancel());
  loginForm.getAnimations().forEach((animation) => animation.cancel());
  registerForm.getAnimations().forEach((animation) => animation.cancel());

  if (reducedMotion) {
    outgoing.hidden = true;
    incoming.hidden = false;
    stage.dataset.active = next;
    stage.classList.remove("is-switching");
    stage.style.height = "";
    focusInput();
    return;
  }

  const direction = next === "register" ? 1 : -1;
  const startHeight = stage.getBoundingClientRect().height;
  incoming.hidden = false;
  stage.style.height = `${startHeight}px`;
  stage.classList.add("is-switching");
  const endHeight = incoming.scrollHeight;
  stage.dataset.active = next;

  const timing = { duration: 380, easing: "cubic-bezier(.22,.78,.2,1)", fill: "both" };
  const heightAnimation = stage.animate(
    [{ height: `${startHeight}px` }, { height: `${endHeight}px` }],
    { duration: 420, easing: "cubic-bezier(.22,.78,.2,1)", fill: "both" },
  );
  const outgoingAnimation = outgoing.animate(
    [
      { opacity: 1, transform: "translateX(0) scale(1)", filter: "blur(0)" },
      { opacity: 0, transform: `translateX(${-direction * 22}px) scale(.985)`, filter: "blur(2px)" },
    ],
    timing,
  );
  const incomingAnimation = incoming.animate(
    [
      { opacity: 0, transform: `translateX(${direction * 26}px) scale(.985)`, filter: "blur(2px)" },
      { opacity: 1, transform: "translateX(0) scale(1)", filter: "blur(0)" },
    ],
    timing,
  );

  Promise.allSettled([heightAnimation.finished, outgoingAnimation.finished, incomingAnimation.finished]).then(() => {
    if (token !== authTabTransitionToken) return;
    outgoing.hidden = true;
    incoming.hidden = false;
    stage.classList.remove("is-switching");
    stage.style.height = "";
    focusInput();
  });
}

function setAuthGate(open, { tab = "login", resume = false } = {}) {
  const gate = $("#auth-gate");
  if (!gate) return;
  if (resume) resumeGenerationAfterAuth = true;
  gate.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  if (open) setAuthTab(tab, { animate: false });
  else {
    resumeGenerationAfterAuth = false;
    const url = new URL(location.href);
    if (url.searchParams.has("auth")) {
      url.searchParams.delete("auth");
      history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }
}

function requireAuthentication({ resume = false, tab = "login" } = {}) {
  if (isAuthenticated) return true;
  setAuthGate(true, { tab, resume });
  return false;
}

function applyAuthenticatedUser(payload) {
  isAuthenticated = true;
  const layout = document.querySelector(".layout");
  if (layout) layout.dataset.authenticated = "1";
  $$(".auth-only").forEach((element) => (element.hidden = false));
  $$(".guest-only").forEach((element) => (element.hidden = true));
  const name = payload.display_name || payload.username || "MURAL 用户";
  const initial = Array.from(name)[0]?.toUpperCase() || "S";
  const menuName = $(".user-menu-account strong");
  const menuMeta = $(".user-menu-account small");
  const profileName = $(".user-profile-copy strong");
  const profileMeta = $(".user-profile-copy small");
  if (menuName) menuName.textContent = name;
  if (menuMeta) menuMeta.textContent = "MURAL Studio 用户";
  if (profileName) profileName.textContent = name;
  if (profileMeta) profileMeta.textContent = "个人账号";
  const menuAvatar = $(".user-menu-account .user-avatar");
  const profileAvatar = $("#user-menu-trigger .user-avatar");
  if (menuAvatar) menuAvatar.textContent = initial;
  if (profileAvatar) profileAvatar.textContent = initial;
}

async function switchAccount() {
  const button = $('[data-user-action="switch"]');
  if (button) button.disabled = true;
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/auth/logout`, {
      method: "POST",
      credentials: "same-origin",
      redirect: "follow",
    });
    if (!response.ok) throw new Error("切换账号失败，请稍后重试");
    // 重新载入游客态页面，既清除上一账号的界面数据，也直接打开登录弹窗。
    location.assign(`${globalThis.STUDIO_BASE_PATH || ""}/?auth=login`);
  } catch (error) {
    if (button) button.disabled = false;
    showStudioNotice(error?.message || "切换账号失败，请稍后重试");
  }
}

async function submitAuthForm(form, endpoint) {
  const submit = form.querySelector('button[type="submit"]');
  const errorBox = $("#auth-gate-error");
  errorBox.hidden = true;
  submit.disabled = true;
  try {
    const response = await fetch(endpoint, { method: "POST", body: new FormData(form) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || payload.detail || "认证失败，请重试");
    const shouldResume = resumeGenerationAfterAuth;
    applyAuthenticatedUser(payload);
    setAuthGate(false);
    form.reset();
    await Promise.all([loadDecks(), loadCustomModels().catch(() => {})]);
    if (shouldResume) await send();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
  }
}

function setTopbar(title = "新对话", subtitle = "AI 生成内容可能存在偏差，请注意核验") {
  const titleEl = $("#topbar-title");
  const subtitleEl = $("#topbar-subtitle");
  if (titleEl) titleEl.textContent = title || "新对话";
  if (subtitleEl) subtitleEl.textContent = subtitle;
}

function setSidebarCollapsed(collapsed, { remember = true } = {}) {
  const layout = document.querySelector(".layout");
  const button = $("#sidebar-toggle");
  const expandButton = $("#sidebar-expand");
  const nextCollapsed = !!collapsed;
  if (nextCollapsed) setUserMenu(false);
  if (!remember) layout?.classList.add("sidebar-transition-suppressed");
  layout?.classList.toggle("sidebar-collapsed", nextCollapsed);
  if (!remember) requestAnimationFrame(() => requestAnimationFrame(() => layout?.classList.remove("sidebar-transition-suppressed")));
  if (button) {
    button.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
    button.setAttribute("aria-label", nextCollapsed ? uiText("expand_sidebar") : uiText("collapse_sidebar"));
    button.title = nextCollapsed ? uiText("expand_sidebar") : uiText("collapse_sidebar");
  }
  if (expandButton) {
    expandButton.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
    expandButton.setAttribute("aria-label", uiText("expand_sidebar"));
    expandButton.title = uiText("expand_sidebar");
  }
  if (remember) localStorage.setItem("studio_sidebar_collapsed", nextCollapsed ? "1" : "0");
}

function setProcRailOpen(open) {
  const editor = $("#editor");
  const button = $("#proc-toggle");
  const nextOpen = !!open;
  editor?.classList.toggle("proc-open", nextOpen);
  button?.setAttribute("aria-expanded", nextOpen ? "true" : "false");
}

function setProcRailCollapsed(collapsed, { remember = true } = {}) {
  const editor = $("#editor");
  const collapseButton = $("#proc-rail-collapse");
  const expandButton = $("#proc-rail-expand");
  const nextCollapsed = !!collapsed;
  if (nextCollapsed) setProcRailOpen(false);
  editor?.classList.toggle("proc-rail-collapsed", nextCollapsed);
  collapseButton?.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
  expandButton?.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
  if (remember) localStorage.setItem("studio_proc_rail_collapsed", nextCollapsed ? "1" : "0");
  requestAnimationFrame(() => scheduleDeckViewportFit());
  setTimeout(() => scheduleDeckViewportFit(), 180);
  setTimeout(() => scheduleDeckViewportFit(), 430);
}

function setTheme(theme, { remember = true } = {}) {
  const safeTheme = theme === "dark" ? "dark" : "light";
  const root = document.documentElement;
  const button = $("#theme-toggle");
  root.dataset.theme = safeTheme;
  root.style.colorScheme = safeTheme;
  if (button) {
    const dark = safeTheme === "dark";
    button.classList.toggle("dark-active", dark);
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    button.setAttribute("aria-label", dark ? "切换亮色主题" : "切换深色主题");
    button.title = dark ? "切换亮色主题" : "切换深色主题";
    const icon = button.querySelector("span");
    if (icon) icon.textContent = dark ? "☀" : "☾";
  }
  $$('[data-theme-choice]').forEach((choice) => {
    const selected = choice.dataset.themeChoice === safeTheme;
    choice.classList.toggle("selected", selected);
    choice.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  if (remember) localStorage.setItem("studio_theme", safeTheme);
}

const UI_COPY = {
  zh: {
    new_chat: "新对话", history: "历史记录", theme: "主题", login_register: "登录 / 注册", new_presentation: "新建演示",
    history_search: "搜索历史记录", no_history_matches: "没有匹配的历史记录",
    batch: "批量生成与下载", add_model: "添加模型", static_presentation: "静态演示",
    dynamic_presentation: "动态演示", switch_account: "切换账号", logout: "退出登录",
    hero_title: "今天，你想展示什么？", start_inspiration: "从一个灵感开始", refresh: "换一批", prompt_placeholder: "描述你的主题、受众和想要的效果…",
    generate: "开始生成", upload_files: "上传文件或图片", upload_limit: "最多 8 个，单个不超过 20MB",
    length: "篇幅", style: "风格", model: "模型", skill: "Skill", appearance: "外观",
    appearance_note: "选择适合当前环境的界面主题", light: "浅色", dark: "深色", language: "语言",
    language_note: "设置 Studio 的界面语言", expand_sidebar: "展开侧栏", collapse_sidebar: "收起侧栏",
    deck_language: "PPT 语言", deck_language_short: "语言", deck_language_auto: "自动（跟随需求）", deck_language_auto_short: "自动",
    mode_static: "已选择静态演示", mode_dynamic: "已选择动态演示",
    static_hint: "静态演示约需 20–35 分钟 · 期间可关闭页面 · ⌘/Ctrl + Enter 发送",
    dynamic_hint: "动态演示会逐页编排动效并渲染检查 · 右侧实时预览 · ⌘/Ctrl + Enter 发送",
  },
  en: {
    new_chat: "New chat", history: "History", theme: "Theme", login_register: "Sign in / Register", new_presentation: "New presentation",
    history_search: "Search history", no_history_matches: "No matching history",
    batch: "Batch generation & download", add_model: "Add model", static_presentation: "Static presentation",
    dynamic_presentation: "Dynamic presentation", switch_account: "Switch account", logout: "Sign out",
    hero_title: "What would you like to present today?", start_inspiration: "Start with an idea", refresh: "Refresh", prompt_placeholder: "Describe your topic, audience, and desired outcome…",
    generate: "Generate", upload_files: "Upload files or images", upload_limit: "Up to 8 files, 20MB each",
    length: "Length", style: "Style", model: "Model", skill: "Skill", appearance: "Appearance",
    appearance_note: "Choose a comfortable interface theme", light: "Light", dark: "Dark", language: "Language",
    language_note: "Set the Studio interface language", expand_sidebar: "Expand sidebar", collapse_sidebar: "Collapse sidebar",
    deck_language: "PPT language", deck_language_short: "Language", deck_language_auto: "Auto (follow prompt)", deck_language_auto_short: "Auto",
    mode_static: "Static presentation selected", mode_dynamic: "Dynamic presentation selected",
    static_hint: "Static generation takes about 20–35 minutes · You may close this page · ⌘/Ctrl + Enter to send",
    dynamic_hint: "Dynamic generation choreographs and checks each slide · Live preview on the right · ⌘/Ctrl + Enter to send",
  },
};
const storedLanguage = localStorage.getItem("studio_language");
const serverDefaultLanguage = document.querySelector(".layout")?.dataset.defaultLanguage === "en" ? "en" : "zh";
let currentLanguage = storedLanguage === "en" || storedLanguage === "zh"
  ? storedLanguage
  : serverDefaultLanguage;
function uiText(key) { return UI_COPY[currentLanguage]?.[key] || UI_COPY.zh[key] || key; }
function setLanguage(language, { remember = true } = {}) {
  currentLanguage = language === "en" ? "en" : "zh";
  document.documentElement.lang = currentLanguage;
  $$('[data-i18n]').forEach((element) => {
    const value = UI_COPY[currentLanguage][element.dataset.i18n];
    if (value) element.textContent = value;
  });
  $$('[data-i18n-placeholder]').forEach((element) => {
    const value = UI_COPY[currentLanguage][element.dataset.i18nPlaceholder];
    if (value) element.placeholder = value;
  });
  const selector = $("#settings-language");
  if (selector) selector.value = currentLanguage;
  syncDeckLanguagePicker($("#deck-language-sel").value);
  if (isAuthenticated && historyRows.length) {
    renderHistoryRows();
    renderHistorySearchResults();
  }
  setCreationMode(creationMode, { remember: false });
  setSidebarCollapsed(document.querySelector(".layout")?.classList.contains("sidebar-collapsed"), { remember: false });
  if (remember) localStorage.setItem("studio_language", currentLanguage);
}

let creationMode = dynamicEnabled && localStorage.getItem("studio_creation_mode") === "dynamic" ? "dynamic" : "static";
let dynamicSkill = "sense-present-dazzle";
function setCreationMode(mode, { remember = true } = {}) {
  const nextMode = dynamicEnabled && mode === "dynamic" ? "dynamic" : "static";
  const changed = nextMode !== creationMode;
  creationMode = nextMode;
  const modeOptions = $(".creation-mode-options");
  if (modeOptions) modeOptions.dataset.active = creationMode;
  const dock = $(".composer-dock");
  if (dock) dock.dataset.mode = creationMode;
  $$(".creation-mode-card").forEach((card) => {
    const on = card.dataset.mode === creationMode;
    card.classList.toggle("selected", on);
    card.setAttribute("aria-pressed", on ? "true" : "false");
  });
  // 两条生成线共用风格、模型和全部偏好；仅 Skill 随类型切换。
  $("#common-settings").hidden = false;
  setSkillMenu(false);
  syncSkillPicker();
  if ($("#mode-summary")) $("#mode-summary").textContent = uiText(creationMode === "static" ? "mode_static" : "mode_dynamic");
  if ($("#generation-hint")) $("#generation-hint").textContent = uiText(creationMode === "static" ? "static_hint" : "dynamic_hint");
  syncAttachZone();
  syncFontControl();
  if (changed && remember) {
    dock?.classList.remove("mode-switching");
    void dock?.offsetWidth;
    dock?.classList.add("mode-switching");
    clearTimeout(window.__studioModeAnimation);
    window.__studioModeAnimation = setTimeout(() => dock?.classList.remove("mode-switching"), 680);
  }
  if (remember) localStorage.setItem("studio_creation_mode", creationMode);
}

function showComposer() {
  $("#editor").hidden = true;
  $("#composer").hidden = false;
  setTopbar();
  renderSuggestions();
  setCreationMode(creationMode, { remember: false });
}

function syncDynamicModels(payload = {}) {
  // /models 探活只作观测，不能作为禁用模型的依据：部分兼容端点不开放
  // /models，且短暂网络抖动也不应阻止用户发出真实生成请求。
  if (!Array.isArray(payload.models)) return;
  const states = Object.fromEntries(payload.models.map((model) => [model.key, model.ok]));
  window.__dynamicModelState = states;
  const select = $("#model-sel");
  if (!select) return;
  [...select.options].forEach((option) => {
    option.dataset.baseLabel ||= option.textContent.replace(/ · 不可达$/, "");
    option.disabled = false;
    option.textContent = option.dataset.baseLabel;
    if (option.value in states) option.dataset.health = states[option.value] ? "ok" : "unknown";
  });
  renderModelMenu();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
async function jget(u, options) { const r = await fetch(u, options); if (!r.ok) throw new Error(await r.text()); return r.json(); }
async function jpatch(u, body) {
  const r = await fetch(u, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let message = await r.text();
    try { message = JSON.parse(message).detail || message; } catch {}
    throw new Error(message);
  }
  return r.json();
}
let customModelItems = [];

function upsertCustomModelOption(model, { select = false } = {}) {
  const main = $("#model-sel");
  const defaultPipeline = main.options[0]?.dataset.defaultPipeline || $("#pipeline-sel")?.value || "";
  [main, $("#batch-model"), $("#service-title-task-model")].filter(Boolean).forEach((target) => {
    let option = [...target.options].find((item) => item.value === model.key);
    if (!option) {
      option = document.createElement("option");
      option.value = model.key;
      target.append(option);
    }
    option.textContent = model.name;
    option.dataset.backend = "openai";
    option.dataset.defaultPipeline = defaultPipeline;
    option.dataset.custom = "1";
    option.dataset.thinkingToggle = model.thinking_toggle ? "1" : "0";
    option.dataset.effortLevels = (model.effort_levels || []).join(",");
    option.dataset.effortWithoutThinking = (model.effort_levels_without_thinking || []).join(",");
    option.dataset.effortDefault = model.thinking_effort || "";
  });
  if (select) {
    main.value = model.key;
    normalizeVersionSelection();
  }
  renderModelMenu();
}

function removeCustomModelOption(key) {
  const main = $("#model-sel");
  const wasSelected = main?.value === key;
  [$("#model-sel"), $("#batch-model"), $("#service-title-task-model")].filter(Boolean).forEach((target) => {
    const option = [...target.options].find((item) => item.value === key);
    if (option) option.remove();
  });
  if (wasSelected || !main.value) {
    main.selectedIndex = main.options.length ? 0 : -1;
    normalizeVersionSelection();
  }
  if (localStorage.getItem("studio_model") === key) localStorage.removeItem("studio_model");
  renderModelMenu();
}

async function deleteCustomModel(key, button = null) {
  const model = customModelItems.find((item) => item.key === key);
  if (!model || !confirm(`删除自定义模型“${model.name}”？历史任务仍会保留。`)) return false;
  if (button) button.disabled = true;
  try {
    const id = key.split(":")[1];
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/models/custom/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "删除失败");
    customModelItems = customModelItems.filter((item) => item.key !== key);
    removeCustomModelOption(key);
    renderCustomModelList();
    showStudioNotice(`已删除模型“${model.name}”`, { type: "success", duration: 3500 });
    return true;
  } catch (error) {
    showStudioNotice(`模型删除失败：${error.message}`, { type: "error", duration: 7000 });
    if (button) button.disabled = false;
    return false;
  }
}

function renderCustomModelList() {
  const section = $("#custom-models-section");
  const list = $("#custom-model-list");
  section.hidden = !customModelItems.length;
  list.innerHTML = customModelItems.map((model) => `
    <div class="custom-model-item" data-model-key="${escapeHtml(model.key)}">
      <span class="custom-model-item-copy"><strong>${escapeHtml(model.name)}</strong><small>${escapeHtml(model.model_id)} · ${escapeHtml(model.base_url)}${model.has_api_key ? " · Key 已保存" : ""}</small></span>
      <button type="button" data-delete-model="${escapeHtml(model.key)}" aria-label="删除 ${escapeHtml(model.name)}">删除</button>
    </div>`).join("");
}

async function loadCustomModels() {
  const payload = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/models/custom`);
  customModelItems = payload.models || [];
  customModelItems.forEach((model) => upsertCustomModelOption(model));
  renderCustomModelList();
}

async function setModelDialog(open) {
  if (open && !requireAuthentication()) return;
  const modal = $("#model-modal");
  modal.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  $("#model-form-error").hidden = true;
  if (open) {
    try { await loadCustomModels(); } catch (error) {
      const box = $("#model-form-error"); box.textContent = "模型列表加载失败：" + error.message; box.hidden = false;
    }
    $("#custom-model-name").focus();
  }
}

let serviceSettings = null;

async function guideOptionalMaterialServices() {
  if (localStorage.getItem("studio_optional_material_services_hint") === "1") return;
  try {
    const settings = serviceSettings || await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/settings/services`);
    const missing = [];
    if (!settings.image_generation?.available) missing.push("AI 生图");
    if (!settings.web_search?.available) missing.push("联网搜图/搜索");
    if (!missing.length) return;
    localStorage.setItem("studio_optional_material_services_hint", "1");
    showStudioNotice(
      `${missing.join("和")}尚未配置；不影响发起任务，Skill 会按现有能力降级。可在左下角“用户 → 设置”中补充。`,
      { type: "info", duration: 10000 },
    );
  } catch (_) {
    // Optional guidance must never block generation.
  }
}

function syncServiceCards() {
  ["image", "search", "visual-critic"].forEach((name) => {
    const enabled = $(`#service-${name}-enabled`)?.checked;
    $(`[data-service-card="${name}"]`)?.classList.toggle("is-enabled", !!enabled);
  });
}

function syncImageProviderFields({ applyDefaults = false } = {}) {
  const provider = $("#service-image-provider")?.value || "openai_images";
  const isSenseNova = provider === "sensenova_u1";
  const url = $("#service-image-url");
  const model = $("#service-image-model");
  const note = $("#service-image-provider-note");
  if (note) note.textContent = isSenseNova
    ? "SenseNova U1 · 原生 Images API"
    : "OpenAI Images API 兼容服务";
  if (url) url.placeholder = isSenseNova
    ? "https://token.sensenova.cn/v1"
    : "https://example.com/v1";
  if (model) model.placeholder = isSenseNova ? "sensenova-u1-fast" : "gpt-image-2";
  if (applyDefaults && isSenseNova && !url.value.trim()) url.value = "https://token.sensenova.cn/v1";
  if (applyDefaults && isSenseNova && model && !model.value.trim()) model.value = "sensenova-u1-fast";
}

function applyServiceSettings(payload) {
  serviceSettings = payload || {};
  const image = serviceSettings.image_generation || {};
  const search = serviceSettings.web_search || {};
  const generation = serviceSettings.generation || {};
  $("#service-max-tokens").value = generation.max_tokens || 40960;
  $("#service-streaming-enabled").checked = generation.streaming_enabled === true;
  $("#service-visual-critic-enabled").checked = generation.enable_visual_critic === true;
  $("#service-static-max-turns").value = generation.static_max_turns ?? 240;
  $("#service-static-subagent-max-turns").value = generation.static_subagent_max_turns ?? 240;
  $("#service-dynamic-max-turns").value = generation.dynamic_max_turns || 4096;
  const titleTaskModel = $("#service-title-task-model");
  const configuredTaskModel = generation.title_task_model || "deployment-model";
  if (titleTaskModel) {
    const available = [...titleTaskModel.options].some((option) => option.value === configuredTaskModel);
    titleTaskModel.value = available
      ? configuredTaskModel
      : ([...titleTaskModel.options].some((option) => option.value === "deployment-model")
        ? "deployment-model" : (titleTaskModel.options[0]?.value || ""));
  }
  $("#service-image-enabled").checked = !!image.enabled;
  $("#service-image-provider").value = image.provider || "openai_images";
  $("#service-image-url").value = image.base_url || "";
  $("#service-image-model").value = image.model || "";
  $("#service-image-key").value = "";
  $("#service-image-key-state").textContent = image.has_api_key ? "已安全保存" : "未配置";
  $("#service-image-clear-wrap").hidden = !image.has_api_key;
  $("#service-image-clear").checked = false;
  $("#service-search-enabled").checked = !!search.enabled;
  $("#service-search-url").value = search.base_url || "";
  $("#service-search-key").value = "";
  $("#service-search-key-state").textContent = search.has_api_key ? "已安全保存" : "未配置";
  $("#service-search-clear-wrap").hidden = !search.has_api_key;
  $("#service-search-clear").checked = false;
  syncImageProviderFields();
  syncServiceCards();
}

async function setServiceDialog(open) {
  if (open && !requireAuthentication()) return;
  const modal = $("#service-modal");
  modal.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  const errorBox = $("#service-form-error");
  errorBox.hidden = true;
  if (!open) return;
  try {
    applyServiceSettings(await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/settings/services`));
  } catch (error) {
    errorBox.textContent = "服务配置加载失败：" + error.message;
    errorBox.hidden = false;
  }
}

const ENVIRONMENT_CHECK_LABELS = {
  skill_source: "当前 Skill 源码",
  skill_snapshot: "固定 Skill 版本",
  skill_scripts: "Skill 命令入口",
  model_credentials: "模型凭证配置（非探活）",
  playwright: "Playwright Python 依赖",
  pillow: "Pillow 图像依赖",
  pymupdf: "PDF 解析依赖",
  rapidocr: "RapidOCR 依赖",
  opencv: "OCR 图像运行库",
  python_docx: "DOCX 解析依赖",
  python_pptx: "PPTX 解析依赖",
  openpyxl: "XLSX 解析依赖",
  libreoffice: "LibreOffice 命令可发现性",
  formal_inference_runtime: "正式 inference 运行环境",
  skill_harness_pair: "Skill / Harness 配对",
  renderer: "Chromium 渲染",
  renderer_prerequisites: "Chromium 运行条件",
  render_broker: "渲染调度器",
  runtime_writable: "运行目录写入",
  fonts: "中文字体",
  serif_font: "中文衬线字体",
  typography_route_fonts: "字体路线",
  main_model_multimodal: "主模型多模态",
  main_model_and_external_vision: "主模型与视觉服务",
  model_configuration: "模型配置",
  independent_visual_verifier: "独立视觉 Critic",
  bounded_web_extract_transport: "网页提取传输",
  requested_attachment_parsers: "附件解析器",
  libreoffice_preview: "Office 预览",
  web_search: "联网搜索",
  image_generate: "AI 生图",
};

function renderEnvironmentCheck(payload = {}) {
  const summary = $("#environment-check-summary");
  const title = $("#environment-check-title");
  const meta = $("#environment-check-meta");
  const list = $("#environment-check-list");
  const status = payload.checked ? (payload.status || "blocked") : "idle";
  summary.className = `environment-check-summary is-${status}`;
  if (!payload.checked) {
    title.textContent = "尚未检验";
    meta.textContent = "选择模型后检查本地运行条件";
    list.innerHTML = "";
    return;
  }
  const titles = {
    ready: payload.live_probes ? "环境可用" : "本地预检通过（未探活外部服务）",
    degraded: "本地环境有可选缺失项",
    blocked: "环境存在阻塞项",
  };
  title.textContent = titles[status] || "环境检验已完成";
  const checkedAt = payload.checked_epoch
    ? new Date(Number(payload.checked_epoch) * 1000).toLocaleString()
    : "";
  const duration = payload.duration_ms ? fmtDur(Number(payload.duration_ms) / 1000) : "";
  const runtime = payload.runtime_aligned === true
    ? "生成 / 附件 / OCR 已对齐"
    : (payload.runtime_aligned === false ? "运行解释器未对齐" : "");
  meta.textContent = [payload.model_label, runtime, checkedAt, duration].filter(Boolean).join(" · ");
  list.innerHTML = (payload.checks || []).map((item) => {
    const label = ENVIRONMENT_CHECK_LABELS[item.id]
      || item.id.replace(/^attachment_/, "附件：").replaceAll("_", " ");
    const latency = item.latency_ms ? `${(item.latency_ms / 1000).toFixed(1)}s` : "";
    const scope = item.required ? "必需" : "可选";
    return `<div class="environment-check-item ${item.ok ? "ok" : "fail"}">
      <i>${item.ok ? "✓" : "!"}</i>
      <span><strong>${escapeHtml(label)} · ${scope}</strong><small>${escapeHtml(item.detail || "")}</small></span>
      <em>${latency}</em>
    </div>`;
  }).join("");
}

async function setEnvironmentDialog(open) {
  if (open && !requireAuthentication()) return;
  const modal = $("#environment-modal");
  modal.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  $("#environment-check-error").hidden = true;
  if (!open) return;
  const currentModel = $("#model-sel")?.value;
  if (currentModel && $(`#environment-model option[value="${CSS.escape(currentModel)}"]`)) {
    $("#environment-model").value = currentModel;
  }
  try {
    renderEnvironmentCheck(await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/settings/environment-check`));
  } catch (error) {
    const box = $("#environment-check-error");
    box.textContent = "上次检验结果加载失败：" + error.message;
    box.hidden = false;
  }
}

async function runEnvironmentCheck() {
  const button = $("#environment-check-run");
  const errorBox = $("#environment-check-error");
  errorBox.hidden = true;
  button.disabled = true;
  button.textContent = "检验中…";
  const summary = $("#environment-check-summary");
  summary.className = "environment-check-summary is-running";
  $("#environment-check-title").textContent = "正在检验运行环境";
  $("#environment-check-meta").textContent = "正在检查当前 v1 的本地依赖与配置，不发送外部模型请求";
  $("#environment-check-list").innerHTML = "";
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/settings/environment-check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("#environment-model").value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "环境检验失败");
    renderEnvironmentCheck(payload);
  } catch (error) {
    summary.className = "environment-check-summary is-blocked";
    $("#environment-check-title").textContent = "环境检验未完成";
    $("#environment-check-meta").textContent = "请检查运行服务后重试";
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "重新检验";
  }
}

function fmtDur(s) {
  if (s == null || !Number.isFinite(Number(s))) return "";
  const total = Math.max(0, Math.round(Number(s)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h) return `${h}小时${m}分${sec}秒`;
  return m ? `${m}分${sec}秒` : `${sec}秒`;
}
// ``ed.kind`` describes the backend transport (legacy dynamic API versus the
// unified Deck API).  The new direct sn-ppt-dazzle route intentionally uses the
// Deck API, so visual identity must come from the produced presentation type.
function editorPresentationKind() {
  return ed.kind === "dynamic" || ed.pptOutput === "dynamic_html" ? "dynamic" : "static";
}
function fileUrl(n) {
  if (ed.kind === "dynamic") return `${globalThis.STUDIO_BASE_PATH || ""}/dynamic/files/${ed.id}/shots/page_${pad2(n)}.png?t=${ed.dynamicStamp || 0}`;
  const t = (ed.rtimes && ed.rtimes[String(n)]) || 0;   // mtime 作缓存戳:重渲后换新图
  if (ed.pptOutput === "dynamic_html") {
    return `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/file?rel=shots/page_${pad2(n)}.png&t=${t}`;
  }
  return trajectoryMode
    ? `${trajectoryDeckApi(ed.id, "/file")}?rel=renders/slide_${pad2(n)}.png&t=${t}`
    : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/file?rel=renders/slide_${pad2(n)}.png&t=${t}`;
}

/* ---------------- 侧栏列表 ---------------- */
const HISTORY_GROUPS = [
  { key: "pinned", label: "置顶", icon: "pin" },
  { key: "static", label: trajectoryMode ? "合成数据" : "静态演示", icon: "static" },
  { key: "dynamic", label: "动态演示", icon: "dynamic" },
].filter((group) => dynamicEnabled || group.key !== "dynamic");
let historyContextTarget = null;
let historyRows = [];
let historySearchActiveIndex = 0;
let historyTitleTimer = null;

function historyTitle(row) {
  return String(row.display_title || row.title || "未命名");
}

function syncStaticTitle(id, payload) {
  if (!payload || !(payload.display_title || payload.title)) return;
  const title = historyTitle(payload);
  const row = historyRows.find((item) => item.kind === "static" && String(item.id) === String(id));
  if (row && historyTitle(row) !== title) {
    row.title = payload.title;
    row.display_title = title;
    row.title_source = payload.title_source ?? row.title_source;
    renderHistoryRows();
    if (!$("#history-search-modal")?.hidden) renderHistorySearchResults();
  }
  if (ed.kind === "static" && String(ed.id) === String(id) && $("#ed-title").textContent !== title) {
    $("#ed-title").textContent = title;
    setTopbar(title, `${editorPresentationKind() === "dynamic" ? "动态" : "静态"}演示 · AI 生成内容请注意核验`);
  }
}

function historySkillKey(row) {
  return String(row.skill_version || "");
}

function historyModelKey(row) {
  return String(row.model || row.model_key || "");
}

function historySkillLabel(row) {
  const key = historySkillKey(row);
  return String(row.skill_label || SKILL_LABEL[key] || (key === "dazzle-deck" ? "Dazzle Deck" : key));
}

function historyModelLabel(row) {
  const key = historyModelKey(row);
  return String(row.model_label || modelLabel(key));
}

function searchedHistoryRows() {
  const query = String($("#history-search-input")?.value || "").trim().toLowerCase();
  if (!query) return historyRows;
  return historyRows.filter((row) => [
    historyTitle(row), historySkillLabel(row), historyModelLabel(row),
    STATUS_LABEL[row.status] || row.status,
  ].some((value) => String(value || "").toLowerCase().includes(query)));
}

function openHistoryRow(row) {
  if (!row) return;
  setHistorySearch(false);
  row.kind === "dynamic" ? openDynamic(row.id) : openDeck(+row.id);
}

function renderHistorySearchResults() {
  const box = $("#history-search-results");
  if (!box) return;
  const rows = searchedHistoryRows();
  historySearchActiveIndex = Math.min(Math.max(0, historySearchActiveIndex), Math.max(0, rows.length - 1));
  if (!rows.length) {
    box.innerHTML = `<div class="history-search-empty">${escapeHtml(uiText("no_history_matches"))}</div>`;
    return;
  }
  box.innerHTML = rows.map((row, index) => {
    const visualKind = row.groupKind || row.kind;
    const kindLabel = visualKind === "dynamic" ? uiText("dynamic_presentation") : uiText("static_presentation");
    const statusLabel = STATUS_LABEL[row.status] || row.status || "";
    const meta = [kindLabel, statusLabel].filter(Boolean).join(" · ");
    return `<button type="button" class="history-search-result${index === historySearchActiveIndex ? " active" : ""}"
      data-history-search-index="${index}" role="option" aria-selected="${index === historySearchActiveIndex ? "true" : "false"}">
      <span class="kind-mark ${visualKind} s-${escapeHtml(String(row.status || ""))}" aria-hidden="true"></span>
      <span class="history-search-result-title">${escapeHtml(historyTitle(row))}</span>
      <span class="history-search-result-meta">${escapeHtml(meta)}</span>
      <span class="history-search-enter" aria-hidden="true">↵</span>
    </button>`;
  }).join("");
}

function setHistorySearch(open) {
  const modal = $("#history-search-modal");
  const trigger = $("#history-search-trigger");
  if (!modal) return;
  modal.hidden = !open;
  trigger?.setAttribute("aria-expanded", open ? "true" : "false");
  document.body.classList.toggle("history-search-open", open);
  if (open) {
    historySearchActiveIndex = 0;
    $("#history-search-input").value = "";
    renderHistorySearchResults();
    requestAnimationFrame(() => $("#history-search-input")?.focus());
  } else if (document.activeElement?.closest("#history-search-modal")) {
    trigger?.focus();
  }
}

function initHistorySearch() {
  const modal = $("#history-search-modal");
  const input = $("#history-search-input");
  $("#history-search-trigger")?.addEventListener("click", () => setHistorySearch(true));
  $("#history-search-close")?.addEventListener("click", () => setHistorySearch(false));
  modal?.addEventListener("click", (event) => {
    if (event.target === modal) setHistorySearch(false);
    const result = event.target.closest("[data-history-search-index]");
    if (result) openHistoryRow(searchedHistoryRows()[+result.dataset.historySearchIndex]);
  });
  input?.addEventListener("input", () => {
    historySearchActiveIndex = 0;
    renderHistorySearchResults();
  });
  input?.addEventListener("keydown", (event) => {
    const rows = searchedHistoryRows();
    if (event.key === "Escape") {
      event.preventDefault();
      setHistorySearch(false);
      return;
    }
    if (!rows.length || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Enter") {
      openHistoryRow(rows[historySearchActiveIndex]);
      return;
    }
    const direction = event.key === "ArrowDown" ? 1 : -1;
    historySearchActiveIndex = (historySearchActiveIndex + direction + rows.length) % rows.length;
    renderHistorySearchResults();
    $("#history-search-results .history-search-result.active")?.scrollIntoView({ block: "nearest" });
  });
}

function historyGroupState() {
  try { return JSON.parse(localStorage.getItem("studio_history_groups") || "{}"); }
  catch { return {}; }
}

function setHistoryGroupCollapsed(key, collapsed) {
  const state = historyGroupState();
  state[key] = !!collapsed;
  localStorage.setItem("studio_history_groups", JSON.stringify(state));
}

function historyRow(d) {
  const visualKind = d.groupKind || d.kind;
  const kindLabel = visualKind === "dynamic" ? uiText("dynamic_presentation") : uiText("static_presentation");
  const statusLabel = STATUS_LABEL[d.status] || d.status;
  const markerLabel = `${kindLabel} · ${statusLabel}`;
  const title = historyTitle(d);
  const active = String(d.id) === String(ed.id) && d.kind === ed.kind;
  return `
    <a class="convo deck-item ${active ? "active" : ""}" data-id="${escapeHtml(String(d.id))}" data-kind="${d.kind}"
      data-title="${escapeHtml(title)}" data-status="${escapeHtml(String(d.status || ""))}"
      data-pinned="${d.pinned ? "1" : "0"}" href="#${d.kind}-${encodeURIComponent(d.id)}">
      <span class="kind-mark ${visualKind} s-${d.status}" role="img" aria-label="${escapeHtml(markerLabel)}" title="${escapeHtml(markerLabel)}"></span>
      <span class="deck-t" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
      <span class="st muted small">${escapeHtml(statusLabel)}</span>
      <button class="del-btn" title="删除这条记录" data-id="${escapeHtml(String(d.id))}" data-kind="${d.kind}">✕</button>
    </a>`;
}

function historyGroupMarkup(group, rows, collapsed) {
  const empty = group.key === "pinned" ? "右键演示可置顶" : `暂无${group.label}`;
  return `<section class="history-group${collapsed ? " collapsed" : ""}" data-history-group="${group.key}">
    <button type="button" class="history-group-head" aria-expanded="${collapsed ? "false" : "true"}">
      <span class="history-group-icon ${group.icon}" aria-hidden="true"></span>
      <span class="history-group-title">${group.label}</span>
      <span class="history-group-count">${rows.length}</span>
      <span class="history-group-chevron" aria-hidden="true"></span>
    </button>
    <div class="history-group-panel"><div class="history-group-items">
      ${rows.length ? rows.map(historyRow).join("") : `<div class="history-group-empty">${empty}</div>`}
    </div></div>
  </section>`;
}

function ensureHistoryContextUi() {
  let menu = $("#history-context-menu");
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "history-context-menu";
    menu.className = "history-context-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    menu.innerHTML = `
      <button type="button" data-history-action="rename" role="menuitem"><span class="history-menu-icon rename" aria-hidden="true"></span><span>重命名</span></button>
      <button type="button" data-history-action="pin" role="menuitem"><span class="history-menu-icon pin" aria-hidden="true"></span><span class="history-pin-label">置顶</span></button>
      <button type="button" data-history-action="regenerate" role="menuitem"><span class="history-menu-icon regenerate" aria-hidden="true"></span><span>重新生成</span></button>`;
    document.body.append(menu);
    menu.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-history-action]")?.dataset.historyAction;
      if (!action || !historyContextTarget) return;
      const target = historyContextTarget;
      closeHistoryContextMenu();
      if (action === "rename") openHistoryRenameDialog(target);
      if (action === "pin") await updateHistoryItem(target, { pinned: !target.pinned });
      if (action === "regenerate") await regenerateHistoryItem(target);
    });
  }
  let modal = $("#history-rename-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "history-rename-modal";
    modal.className = "history-rename-modal";
    modal.hidden = true;
    modal.innerHTML = `<form class="history-rename-card">
      <div class="history-rename-heading"><span>重命名演示</span><button type="button" data-history-rename-close aria-label="关闭">×</button></div>
      <label for="history-rename-input">名称</label>
      <input id="history-rename-input" maxlength="80" autocomplete="off">
      <div class="history-rename-error" role="alert" hidden></div>
      <div class="history-rename-actions"><button type="button" data-history-rename-close>取消</button><button type="submit">保存</button></div>
    </form>`;
    document.body.append(modal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal || event.target.closest("[data-history-rename-close]")) closeHistoryRenameDialog();
    });
    modal.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!historyContextTarget) return;
      const input = $("#history-rename-input");
      const title = input.value.trim();
      const error = modal.querySelector(".history-rename-error");
      if (!title) { error.textContent = "名称不能为空"; error.hidden = false; input.focus(); return; }
      const submit = modal.querySelector('[type="submit"]');
      submit.disabled = true; submit.textContent = "保存中…";
      const ok = await updateHistoryItem(historyContextTarget, { title }, { quiet: true });
      submit.disabled = false; submit.textContent = "保存";
      if (ok) closeHistoryRenameDialog();
      else { error.textContent = "保存失败，请稍后重试"; error.hidden = false; }
    });
  }
  return menu;
}

function openHistoryContextMenu(item, x, y) {
  historyContextTarget = {
    id: item.dataset.id,
    kind: item.dataset.kind,
    title: item.dataset.title || "未命名",
    status: item.dataset.status || "",
    pinned: item.dataset.pinned === "1",
  };
  const menu = ensureHistoryContextUi();
  menu.querySelector(".history-pin-label").textContent = historyContextTarget.pinned ? "取消置顶" : "置顶";
  menu.querySelector('[data-history-action="regenerate"]').hidden = historyContextTarget.status !== "completed";
  menu.hidden = false;
  menu.style.left = `${Math.min(x, innerWidth - menu.offsetWidth - 10)}px`;
  menu.style.top = `${Math.min(y, innerHeight - menu.offsetHeight - 10)}px`;
  requestAnimationFrame(() => menu.classList.add("open"));
}

function closeHistoryContextMenu() {
  const menu = $("#history-context-menu");
  if (!menu) return;
  menu.classList.remove("open");
  menu.hidden = true;
}

function openHistoryRenameDialog(target) {
  historyContextTarget = target;
  ensureHistoryContextUi();
  const modal = $("#history-rename-modal");
  const input = $("#history-rename-input");
  modal.querySelector(".history-rename-error").hidden = true;
  input.value = target.title;
  modal.hidden = false;
  requestAnimationFrame(() => { modal.classList.add("open"); input.focus(); input.select(); });
}

function closeHistoryRenameDialog() {
  const modal = $("#history-rename-modal");
  if (!modal) return;
  modal.classList.remove("open");
  modal.hidden = true;
  historyContextTarget = null;
}

async function updateHistoryItem(target, changes, { quiet = false } = {}) {
  try {
    await jpatch(`${globalThis.STUDIO_BASE_PATH || ""}/api/history/${target.kind}/${encodeURIComponent(target.id)}`, changes);
    if (changes.title && String(ed.id) === String(target.id) && ed.kind === target.kind) {
      $("#ed-title").textContent = changes.title;
    }
    await loadDecks(ed.id);
    return true;
  } catch (error) {
    if (!quiet) alert(`更新失败：${error.message}`);
    return false;
  }
}

async function regenerateHistoryItem(target) {
  try {
    const response = target.kind === "dynamic"
      ? await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/regenerate`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conv_id: target.id }),
        })
      : await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(target.id)}/regenerate`, { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || "重新生成失败");
    if (target.kind === "dynamic") await openDynamic(payload.conv_id, { subscribeNow: true });
    else await openDeck(payload.deck_id);
    refreshDeckListWhenIdle(target.kind === "dynamic" ? payload.conv_id : payload.deck_id);
  } catch (error) {
    alert(`重新生成失败：${error.message}`);
  }
}

function renderHistoryRows() {
  const box = $("#deck-list");
  if (!box) return;
  const rows = historyRows;
  const state = historyGroupState();
  const grouped = {
    pinned: rows.filter((d) => d.pinned),
    static: rows.filter((d) => !d.pinned && d.groupKind === "static"),
    dynamic: dynamicEnabled ? rows.filter((d) => !d.pinned && d.groupKind === "dynamic") : [],
  };
  box.innerHTML = HISTORY_GROUPS.map((group) => historyGroupMarkup(group, grouped[group.key], !!state[group.key])).join("");
  $$("#deck-list .history-group-head").forEach((button) => (button.onclick = () => {
    const section = button.closest(".history-group");
    const collapsed = !section.classList.contains("collapsed");
    section.classList.toggle("collapsed", collapsed);
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    setHistoryGroupCollapsed(section.dataset.historyGroup, collapsed);
  }));
  $$("#deck-list .deck-item").forEach((item) =>
    (item.onclick = (event) => {
      if (event.target.classList.contains("del-btn")) return;
      event.preventDefault();
      item.dataset.kind === "dynamic" ? openDynamic(item.dataset.id) : openDeck(+item.dataset.id);
    }));
  $$("#deck-list .deck-item").forEach((item) => (item.oncontextmenu = trajectoryMode ? null : (event) => {
    event.preventDefault(); event.stopPropagation(); openHistoryContextMenu(item, event.clientX, event.clientY);
  }));
  $$("#deck-list .del-btn").forEach((button) => {
    if (trajectoryMode) { button.hidden = true; return; }
    button.onclick = (event) => {
      event.preventDefault(); event.stopPropagation();
      askDelete(button.closest(".deck-item"), button.dataset.id, button.dataset.kind);
    };
  });
}

async function loadDecks(active) {
  clearTimeout(historyTitleTimer);
  historyTitleTimer = null;
  const box = $("#deck-list");
  const searchTrigger = $("#history-search-trigger");
  if (searchTrigger) searchTrigger.hidden = !isAuthenticated;
  if (!isAuthenticated) {
    historyRows = [];
    box.innerHTML = '<button type="button" class="guest-history" data-open-auth>登录后查看历史演示</button>';
    return;
  }
  try {
    const [staticResult, dynamicResult] = await Promise.all([
      jget(trajectoryMode ? `${globalThis.STUDIO_BASE_PATH || ""}/api/trajectory-monitor/decks` : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks`).catch(() => ({ decks: [] })),
      dynamicEnabled
        ? jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/conversations`).catch(() => ({ items: [] }))
        : Promise.resolve({ items: [] }),
    ]);
    syncDynamicModels(dynamicResult);
    const rows = [
      ...(staticResult.decks || [])
        .filter((d) => dynamicEnabled || (d.presentation_kind || "static") === "static")
        .map((d) => ({
        ...d, kind: "static", groupKind: d.presentation_kind || "static",
        sortAt: d.created_at || d.updated_at || 0,
      })),
      ...(dynamicResult.items || []).map((d) => ({
        ...d, id: d.conv_id, kind: "dynamic", groupKind: "dynamic",
        sortAt: d.updated || d.created || "",
      })),
    ];
    if (!trajectoryMode) rows.sort((a, b) => String(b.sortAt).localeCompare(String(a.sortAt)));
    historyRows = rows;
    syncStaticTitle(ed.id, rows.find((row) => row.kind === "static" && String(row.id) === String(ed.id)));
    renderHistoryRows();
    if (!$("#history-search-modal")?.hidden) renderHistorySearchResults();
    if (rows.some((row) => row.title_refresh_pending)) {
      historyTitleTimer = setTimeout(() => loadDecks(ed.id), 2000);
    }
  } catch { box.innerHTML = '<div class="muted small pad">加载失败</div>'; }
}

function openDeckFromLocationHash() {
  if (ed.id) return;
  const match = location.hash.match(dynamicEnabled ? /^#(static|dynamic)-(.+)$/ : /^#(static)-(.+)$/);
  let requestedId = "";
  try { requestedId = match ? decodeURIComponent(match[2]) : ""; } catch { requestedId = ""; }
  const requested = match && $$("#deck-list .deck-item").find((item) =>
    item.dataset.kind === match[1] && item.dataset.id === requestedId);
  if (requested) requested.click();
  else if (trajectoryMode && requestedId) openDeck(+requestedId);
  else if (trajectoryMode) $("#deck-list .deck-item")?.click();
}

function askDelete(row, id, kind = "static") {
  if (!row || row.querySelector(".confirm-box")) return;
  row.classList.add("confirming");
  const box = document.createElement("span");
  box.className = "confirm-box";
  box.innerHTML = `<span class="cf-txt">不可恢复</span>
    <button class="cf-yes">删除</button><button class="cf-no">取消</button>`;
  row.appendChild(box);
  const dismiss = () => {
    box.remove(); row.classList.remove("confirming");
    document.removeEventListener("click", onDocClick, true);
  };
  const onDocClick = (ev) => { if (!box.contains(ev.target)) dismiss(); };
  box.querySelector(".cf-no").onclick = (e) => { e.preventDefault(); e.stopPropagation(); dismiss(); };
  box.querySelector(".cf-yes").onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    const btn = e.target; btn.disabled = true; btn.textContent = "删除中…";
    await removeDeck(id, kind);
    dismiss();
  };
  setTimeout(() => document.addEventListener("click", onDocClick, true), 0);
}

async function removeDeck(id, kind = "static") {
  try {
    const r = kind === "dynamic"
      ? await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conv_id: id }) })
      : await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) { alert("删除失败: " + e.message); return; }
  if (String(ed.id) === String(id)) closeEditor();
  loadDecks(ed.id);
}

/* ---------------- 提交简报 ---------------- */
const MODEL_LABEL = {
  "deployment-model": "PPTAgent Qwen3.5 27B (ckpt1764)",
  "deployment-model-2": "PPTAgent 2",
  "sensenova-flash-lite-v39": "SenseNova Flash Lite v39 (1)",
  "sensenova-flash-lite-v39-2": "SenseNova Flash Lite v39 (2)",
  "sensenova-flash-lite-v39-3": "SenseNova Flash Lite v39 (3)",
  "sensenova-flash-lite-v39-4": "SenseNova Flash Lite v39 (4)",
  "gpt-5.6-sol": "GPT-5.6 SOL (tokenhub)",
  "gpt-5.6-luna": "GPT-5.6 LUNA (tokenhub)",
  "gpt-5.6-terra": "GPT-5.6 TERRA (tokenhub)",
  "kimi-k3": "Kimi K3 (DashScope)",
};
function modelLabel(key) {
  return MODEL_LABEL[key] || key;
}
const PIPELINE_LABEL = {
  infer: "Clean infer harness",
  "visual-craft-harness": "Visual Craft Harness",
  "visual-craft-v3-pre-speech-harness": "Visual Craft V3 · Pre-Speech Harness",
  "visual-craft-v3-speech-harness": "Visual Craft V3 · Speech Harness",
  "sn-ppt-web-harness": "sn-ppt-web harness",
  "mural-presenter-harness": "MURAL Presenter harness",
  "mural-presenter-v0.1-harness": "MURAL Presenter v0.1",
  "mural-presenter-v0.2-harness": "MURAL Presenter v0.2 · Single",
  "mural-presenter-v0.2-grouped-harness": "MURAL Presenter v0.2 · Grouped",
  "mural-presenter-v0.3-harness": "MURAL Presenter v0.3 · Adaptive Ownership",
  "mural-presenter-v0.4-harness": "MURAL Presenter v0.4 · Adaptive Quality",
  "mural-next-harness": "MURAL Next experimental harness",
  "sense-present-standard-harness": "MURAL Static HTML Harness",
  "sense-present-dazzle-harness": "MURAL Dynamic HTML Harness",
  "sense-present-ppt-skill-harness": "Sense Present PPT Skill Suite Harness",
};
const SKILL_LABEL = {
  "sense-present-standard": "MURAL Static HTML",
  "sense-present-dazzle": "MURAL Dynamic HTML",
  "sense-present-ppt-skill": "Sense Present PPT Skill Suite",
  auto: "Auto（自动选择）",
  zh: "中文 Skill",
  en: "English Skill",
  "long-horizon": "Long-horizon HTML PPT",
  "long-horizon-grouped": "Long-horizon HTML PPT Grouped",
  "long-horizon-grouped-inline-image": "Long-horizon Grouped · Inline Image",
  "visual-craft": "Visual Craft HTML PPT",
  "visual-craft-v3-pre-speech": "Visual Craft V3 · Pre-Speech",
  "visual-craft-v3-speech": "Visual Craft V3 · Speech",
  "sn-ppt-web": "sn-ppt-web",
  "mural-presenter": "MURAL Presenter",
  "mural-presenter-creative": "MURAL Presenter · Creative",
  "mural-presenter-v0.1": "MURAL Presenter v0.1",
  "mural-presenter-v0.2": "MURAL Presenter v0.2 · Single",
  "mural-presenter-v0.2-grouped": "MURAL Presenter v0.2 · Grouped",
  "mural-presenter-v0.3": "MURAL Presenter v0.3 · Adaptive Ownership",
  "mural-presenter-v0.4": "MURAL Presenter v0.4 · Adaptive Quality",
  "mural-next": "MURAL Next · Experimental",
};

/* ---------------- 附件(随所选管线能力联动) ---------------- */
let attachFiles = [];               // composer 已选附件(File);仅所选管线 caps 含 attachments 时可用
let fontFiles = [];                 // 用户授权上传的字体；与普通材料分开提交和挂载
const FONT_ROLE_OPTIONS = {
  display: ["Inter Tight", "Geist", "Poppins", "Plus Jakarta Sans", "Space Grotesk", "Sora", "Bebas Neue", "Playfair Display", "Smiley Sans", "ZCOOL XiaoWei"],
  title: ["Noto Sans SC", "Noto Serif SC", "Inter", "Inter Tight", "Geist", "Poppins", "Figtree", "Smiley Sans", "ZCOOL XiaoWei", "Ma Shan Zheng", "Bebas Neue", "Playfair Display", "Space Grotesk", "Sora"],
  body: ["Noto Sans SC", "Noto Serif SC", "Inter", "Open Sans", "Roboto", "Figtree", "Plus Jakarta Sans", "LXGW WenKai", "DM Sans", "Manrope"],
  number: ["IBM Plex Mono", "JetBrains Mono", "Geist Mono", "Roboto Mono", "Archivo", "Montserrat", "Oswald", "Bebas Neue", "Sora"],
  annotation: ["LXGW WenKai", "Zhi Mang Xing", "Dancing Script", "Kalam", "Caveat"],
  mono: ["IBM Plex Mono", "JetBrains Mono", "Geist Mono", "Roboto Mono"],
};
const attachmentObjectUrls = new Map();
let attachmentViewerRequestId = 0;
function pipelineHasCap(cap) {
  const opt = $("#pipeline-sel").selectedOptions && $("#pipeline-sel").selectedOptions[0];
  return (opt?.dataset.caps || "").split(",").includes(cap);
}
function attachmentExtension(name = "") {
  const clean = String(name).split(/[?#]/, 1)[0];
  return clean.includes(".") ? clean.split(".").pop().toLowerCase() : "file";
}
function attachmentPreviewKind(name = "", type = "") {
  const mime = String(type || "").toLowerCase();
  const ext = attachmentExtension(name);
  if ((mime.startsWith("image/") && mime !== "image/svg+xml") || ["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(ext)) return "image";
  if (mime === "application/pdf" || ext === "pdf") return "pdf";
  if (mime === "text/markdown" || ["md", "markdown"].includes(ext)) return "markdown";
  if (mime.startsWith("text/") || ["txt", "csv", "json", "yaml", "yml", "xml", "html", "htm"].includes(ext)) return "text";
  if (mime.startsWith("audio/") || ["mp3", "wav", "m4a", "aac", "ogg", "flac"].includes(ext)) return "audio";
  if (mime.startsWith("video/") || ["mp4", "webm", "mov", "m4v", "avi"].includes(ext)) return "video";
  if (["doc", "docx", "ppt", "pptx", "xls", "xlsx", "xlsm"].includes(ext)) return "office";
  return "file";
}
function attachmentTypeLabel(name = "", type = "") {
  const ext = attachmentExtension(name);
  const kind = attachmentPreviewKind(name, type);
  if (kind === "image") return `${ext === "file" ? "图片" : ext.toUpperCase()} 图片`;
  if (kind === "pdf") return "PDF 文档";
  if (kind === "markdown") return "Markdown 文档";
  if (kind === "text") return `${ext === "file" ? "文本" : ext.toUpperCase()} 文本`;
  if (kind === "audio") return `${ext === "file" ? "音频" : ext.toUpperCase()} 音频`;
  if (kind === "video") return `${ext === "file" ? "视频" : ext.toUpperCase()} 视频`;
  if (kind === "office") return `${ext.toUpperCase()} 文档`;
  return ext === "file" ? "附件" : `${ext.toUpperCase()} 文件`;
}
function attachmentSizeLabel(size = 0) {
  const bytes = Number(size) || 0;
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}
function deckAttachmentPreviewUrls(item = {}, index = 0) {
  if (!/^\d+$/.test(String(ed.id || ""))) return { url: item.url || "", fallbackUrl: "", previewUrl: "" };
  const dedicatedUrl = `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/attachments/${index}`;
  let rel = String(item.preview_rel || "").replace(/^\/+/, "");
  // The dedicated endpoint is the canonical original attachment. Workspace
  // paths are optional derived previews and may disappear after pipeline
  // staging, so they must never be the primary PDF/open-in-new-window URL.
  const workspaceUrl = rel
    ? `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/file?rel=${encodeURIComponent(rel)}`
    : "";
  const suppliedUrl = String(item.url || "");
  return {
    url: suppliedUrl || dedicatedUrl,
    fallbackUrl: suppliedUrl ? dedicatedUrl : workspaceUrl,
    previewUrl: `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/attachments/${index}/preview`,
  };
}
function attachmentObjectUrl(file) {
  if (!attachmentObjectUrls.has(file)) attachmentObjectUrls.set(file, URL.createObjectURL(file));
  return attachmentObjectUrls.get(file);
}
function releaseAttachmentObjectUrl(file) {
  const url = attachmentObjectUrls.get(file);
  if (url) URL.revokeObjectURL(url);
  attachmentObjectUrls.delete(file);
}
function clearAttachFiles() {
  attachFiles.forEach(releaseAttachmentObjectUrl);
  attachFiles = [];
  renderAttachList();
}
function closeAttachmentViewer() {
  attachmentViewerRequestId += 1;
  const viewer = $("#attachment-viewer");
  if (!viewer) return;
  viewer.hidden = true;
  $("#attachment-viewer-body").replaceChildren();
  if ($("#model-modal")?.hidden !== false && $("#service-modal")?.hidden !== false) {
    document.body.classList.remove("modal-open");
  }
}
async function fetchAttachmentSource(url, fallbackUrl = "") {
  let sourceUrl = url;
  let response = await fetch(sourceUrl, { credentials: "same-origin" });
  if (!response.ok && fallbackUrl && fallbackUrl !== sourceUrl) {
    sourceUrl = fallbackUrl;
    response = await fetch(sourceUrl, { credentials: "same-origin" });
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return { sourceUrl, response };
}
function attachmentPreviewError(message = "读取附件失败") {
  const empty = document.createElement("div");
  empty.className = "attachment-viewer-file error";
  empty.innerHTML = `<span>!</span><strong>暂时无法预览</strong><p>${escapeHtml(message)}</p>`;
  return empty;
}
async function openAttachmentViewer({ name = "附件", type = "", size = 0, url = "", fallbackUrl = "", previewUrl = "", downloadUrl = "" } = {}) {
  if (!url) return;
  const requestId = ++attachmentViewerRequestId;
  const viewer = $("#attachment-viewer");
  const body = $("#attachment-viewer-body");
  if (!viewer || !body) return;
  const kind = attachmentPreviewKind(name, type);
  $("#attachment-viewer-title").textContent = name;
  $("#attachment-viewer-meta").textContent = [attachmentTypeLabel(name, type), attachmentSizeLabel(size)].filter(Boolean).join(" · ");
  const marks = { image: "▧", pdf: "PDF", markdown: "MD", text: "TXT", audio: "♪", video: "▶", office: attachmentExtension(name).slice(0, 4).toUpperCase() };
  $("#attachment-viewer-mark").textContent = marks[kind] || "↥";
  body.replaceChildren();
  const open = $("#attachment-viewer-open");
  const download = $("#attachment-viewer-download");
  open.href = url;
  download.href = downloadUrl || url;
  download.download = name;
  viewer.hidden = false;
  document.body.classList.add("modal-open");
  if (kind === "image") {
    const image = document.createElement("img");
    image.src = url;
    image.alt = name;
    if (fallbackUrl && fallbackUrl !== url) {
      image.onerror = () => {
        image.onerror = null;
        image.src = fallbackUrl;
        open.href = fallbackUrl;
        download.href = `${fallbackUrl}${fallbackUrl.includes("?") ? "&" : "?"}download=1`;
      };
    }
    body.append(image);
    $("#attachment-viewer-tip").textContent = "点击外部区域可关闭预览";
  } else if (kind === "markdown") {
    const article = document.createElement("article");
    article.className = "attachment-markdown agent-markdown loading";
    article.innerHTML = "<p>正在整理 Markdown 内容…</p>";
    body.append(article);
    $("#attachment-viewer-tip").textContent = "Markdown 预览";
    try {
      const { sourceUrl, response } = await fetchAttachmentSource(url, fallbackUrl);
      const markdown = await response.text();
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.classList.remove("loading");
      article.innerHTML = agentMarkdownHtml(markdown) || "<p>这个 Markdown 文件暂时没有内容。</p>";
      body.scrollTop = 0;
      if (sourceUrl !== url) open.href = sourceUrl;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.classList.remove("loading");
      article.classList.add("error");
      article.innerHTML = `<h2>暂时无法预览</h2><p>${escapeHtml(error?.message || "读取 Markdown 文件失败")}</p>`;
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else if (kind === "pdf") {
    const frame = document.createElement("iframe");
    frame.src = `${url}${url.includes("#") ? "&" : "#"}view=FitH&toolbar=1&navpanes=0`;
    frame.title = `${name} 预览`;
    frame.className = "attachment-pdf-frame";
    body.append(frame);
    $("#attachment-viewer-tip").textContent = "PDF 预览 · 支持翻页与缩放";
  } else if (kind === "text") {
    const pre = document.createElement("pre");
    pre.className = "attachment-text loading";
    pre.textContent = "正在读取文本内容…";
    body.append(pre);
    $("#attachment-viewer-tip").textContent = "文本内容预览";
    try {
      const { sourceUrl, response } = await fetchAttachmentSource(url, fallbackUrl);
      let content = await response.text();
      if (attachmentExtension(name) === "json") {
        try { content = JSON.stringify(JSON.parse(content), null, 2); } catch (_) { /* keep source */ }
      }
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      pre.classList.remove("loading");
      pre.textContent = content || "这个文件暂时没有文本内容。";
      body.scrollTop = 0;
      if (sourceUrl !== url) open.href = sourceUrl;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      pre.replaceWith(attachmentPreviewError(error?.message || "读取文本文件失败"));
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else if (kind === "audio" || kind === "video") {
    const media = document.createElement(kind);
    media.src = url;
    media.controls = true;
    media.preload = "metadata";
    if (kind === "video") media.playsInline = true;
    body.append(media);
    $("#attachment-viewer-tip").textContent = kind === "audio" ? "音频预览" : "视频预览";
  } else if (kind === "office" && previewUrl) {
    const article = document.createElement("article");
    article.className = "attachment-document loading";
    article.innerHTML = "<p>正在整理文档内容…</p>";
    body.append(article);
    $("#attachment-viewer-tip").textContent = "文档内容预览";
    try {
      const response = await fetch(previewUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      const gallery = (data.assets || []).length ? `<div class="attachment-document-pages">${data.assets.map((asset) => `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.label || name)}" loading="lazy">`).join("")}</div>` : "";
      const text = String(data.text || "").trim();
      const notes = (data.notes || []).filter(Boolean);
      article.classList.remove("loading");
      article.innerHTML = `${gallery}${text ? `<pre>${escapeHtml(text)}</pre>` : ""}${notes.length ? `<div class="attachment-document-notes">${notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}</div>` : ""}${!gallery && !text ? "<p>没有可提取的网页预览内容，请下载后使用对应应用打开。</p>" : ""}`;
      body.scrollTop = 0;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.replaceWith(attachmentPreviewError(error?.message || "读取文档预览失败"));
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else {
    const empty = document.createElement("div");
    empty.className = "attachment-viewer-file";
    empty.innerHTML = `<span>${escapeHtml(attachmentExtension(name).slice(0, 5).toUpperCase())}</span><strong>${escapeHtml(name)}</strong><p>浏览器无法直接预览此格式，可以下载后使用对应应用打开。</p>`;
    body.append(empty);
    $("#attachment-viewer-tip").textContent = "该格式暂不支持网页内预览";
  }
}
function renderAttachList() {
  const box = $("#attach-list"); if (!box) return;
  box.hidden = attachFiles.length === 0;
  box.innerHTML = attachFiles.map((file, index) => {
    const kind = attachmentPreviewKind(file.name, file.type);
    const url = attachmentObjectUrl(file);
    const visual = kind === "image"
      ? `<img src="${escapeHtml(url)}" alt="">`
      : `<span>${kind === "pdf" ? "PDF" : attachmentExtension(file.name).slice(0, 4).toUpperCase()}</span>`;
    const meta = [attachmentTypeLabel(file.name, file.type), attachmentSizeLabel(file.size)].filter(Boolean).join(" · ");
    return `<article class="attach-item" data-i="${index}">
      <button type="button" class="attach-open" data-local-attachment="${index}" title="查看 ${escapeHtml(file.name)}">
        <span class="attach-thumb ${kind}">${visual}</span>
        <span class="attach-copy"><strong>${escapeHtml(file.name)}</strong><small>${escapeHtml(meta)}</small></span>
      </button>
      <button type="button" data-i="${index}" class="attach-x" title="移除 ${escapeHtml(file.name)}" aria-label="移除 ${escapeHtml(file.name)}">×</button>
    </article>`;
  }).join("");
  const count = $("#attach-count");
  if (count) {
    count.textContent = String(attachFiles.length);
    count.hidden = attachFiles.length === 0;
  }
}
function setAttachMenu(open) {
  const zone = $("#attach-zone");
  const menu = $("#attach-menu");
  const trigger = $("#attach-btn");
  if (!zone || !menu || !trigger) return;
  menu.hidden = !open;
  zone.classList.toggle("menu-open", open);
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
function syncAttachZone() {          // 附件仅开放给静态管线；动态演示不展示入口，也不保留待提交附件
  const on = creationMode === "static" && pipelineHasCap("attachments");
  const zone = $("#attach-zone"); if (zone) zone.hidden = !on;
  if (!on) setAttachMenu(false);
  if (!on && attachFiles.length) clearAttachFiles();
}
let fontPanelPositionFrame = 0;

function positionFloatingFontPanel() {
  const panel = $("#font-panel");
  const trigger = $("#font-trigger");
  if (!panel || !trigger || panel.hidden || !panel.classList.contains("font-panel-floating")) return;

  const margin = 12;
  const gap = 12;
  const triggerRect = trigger.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const viewportWidth = window.visualViewport?.width || window.innerWidth;
  const viewportHeight = window.visualViewport?.height || window.innerHeight;

  // Prefer opening to the right of the settings menu. If the viewport is
  // narrow, keep the complete panel inside the visible area instead.
  const preferredLeft = triggerRect.right + gap;
  const maxLeft = Math.max(margin, viewportWidth - panelRect.width - margin);
  const left = Math.max(margin, Math.min(preferredLeft, maxLeft));
  const preferredTop = triggerRect.bottom - panelRect.height;
  const maxTop = Math.max(margin, viewportHeight - panelRect.height - margin);
  const top = Math.max(margin, Math.min(preferredTop, maxTop));

  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

function queueFontPanelPosition() {
  cancelAnimationFrame(fontPanelPositionFrame);
  fontPanelPositionFrame = requestAnimationFrame(positionFloatingFontPanel);
}

function setFontPanel(open) {
  const panel = $("#font-panel"); const trigger = $("#font-trigger");
  const home = $("#font-control");
  if (!panel || !trigger || !home) return;
  const next = !!open;

  if (next) {
    // The sidebar intentionally clips its contents during collapse animation.
    // Portalling this panel to <body> keeps font upload and role selectors
    // fully visible without weakening the sidebar's overflow contract.
    if (panel.parentElement !== document.body) document.body.appendChild(panel);
    panel.classList.add("font-panel-floating");
    panel.hidden = false;
    positionFloatingFontPanel();
    queueFontPanelPosition();
  } else {
    panel.hidden = true;
    panel.classList.remove("font-panel-floating");
    panel.removeAttribute("style");
    if (panel.parentElement !== home) home.appendChild(panel);
  }
  trigger.classList.toggle("active", next);
  trigger.setAttribute("aria-expanded", next ? "true" : "false");
}

window.addEventListener("resize", queueFontPanelPosition);
window.visualViewport?.addEventListener("resize", queueFontPanelPosition);
function ensureFontPreviewStylesheet() {
  // Load the shared built-in preview faces once so the role dropdowns can
  // preview each family. Same resource the deck preview iframe uses, so the
  // config preview and the exported deck resolve identical sources.
  if (document.getElementById("studio-font-preview-css")) return;
  const link = document.createElement("link");
  link.id = "studio-font-preview-css";
  link.rel = "stylesheet";
  link.href = `${globalThis.STUDIO_BASE_PATH || ""}/api/preview-fonts.css`;
  document.head.appendChild(link);
}
function renderFontControls() {
  ensureFontPreviewStylesheet();
  $$('[data-font-role]').forEach((select) => {
    const previous = select.value || "auto";
    const role = select.dataset.fontRole;
    const options = ['<option value="auto">智慧匹配</option>'];
    (FONT_ROLE_OPTIONS[role] || []).forEach((family) => options.push(
      `<option value="builtin:${escapeHtml(family)}" style="font-family:${escapeHtml(JSON.stringify(family))}">${escapeHtml(family)}</option>`
    ));
    fontFiles.forEach((file) => options.push(
      `<option value="custom:${escapeHtml(file.name)}">用户字体 · ${escapeHtml(file.name)}</option>`
    ));
    select.innerHTML = options.join("");
    select.value = [...select.options].some((item) => item.value === previous) ? previous : "auto";
  });
  const list = $("#font-file-list");
  if (list) {
    list.hidden = fontFiles.length === 0;
    list.innerHTML = fontFiles.map((file, index) =>
      `<span class="font-file-chip"><span>${escapeHtml(file.name)}</span><button type="button" data-font-remove="${index}" aria-label="移除字体">×</button></span>`
    ).join("");
  }
  if ($("#font-license")) $("#font-license").hidden = fontFiles.length === 0;
  const chosen = $$('[data-font-role]').filter((select) => select.value !== "auto").length;
  if ($("#font-current")) $("#font-current").textContent = chosen || fontFiles.length ? `${chosen} 个角色` : "智慧匹配";
}
function clearFontFiles() {
  fontFiles = [];
  if ($("#font-license-ack")) $("#font-license-ack").checked = false;
  renderFontControls();
}
function fontRoleConfig() {
  return Object.fromEntries(
    $$('[data-font-role]').map((select) => [select.dataset.fontRole, select.value])
      .filter(([, value]) => value && value !== "auto")
  );
}
function syncFontControl() {
  const enabled = creationMode === "static" && pipelineHasCap("custom_fonts");
  const control = $("#font-control");
  if (control) control.hidden = !enabled;
  if (!enabled) setFontPanel(false);
  if (!enabled && fontFiles.length) clearFontFiles();
}
function thinkingEnabled() {
  return modelSupportsThinking() && !!$("#thinking-sel")?.checked;
}

function effortLevels(option = null) {
  const selected = option || $("#model-sel")?.selectedOptions?.[0];
  return (selected?.dataset.effortLevels || "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
}

function selectedEffort() {
  const levels = selectableEffortLevels();
  const value = $("#effort-sel")?.value || "";
  return levels.includes(value) ? value : "";
}

function selectableEffortLevels(option = null) {
  const selected = option || $("#model-sel")?.selectedOptions?.[0];
  if (selected?.dataset.thinkingToggle === "1" && !thinkingEnabled()) {
    const allowed = (selected.dataset.effortWithoutThinking || "").split(",");
    return effortLevels(selected).filter((level) => allowed.includes(level));
  }
  return effortLevels(selected);
}

function syncEffortControl() {
  const control = $("#effort-control");
  const select = $("#effort-sel");
  const option = $("#model-sel")?.selectedOptions?.[0];
  if (!control || !select || !option) return;
  const levels = selectableEffortLevels(option);
  const available = levels.length > 0;
  control.hidden = !available;
  select.disabled = !available;
  if (!levels.length) {
    select.replaceChildren();
    return;
  }
  const saved = localStorage.getItem(`studio_effort_${option.value}`);
  const preferred = levels.includes(saved)
    ? saved
    : (levels.includes(option.dataset.effortDefault) ? option.dataset.effortDefault : levels[0]);
  select.innerHTML = levels.map((level) => `<option value="${level}">${level}</option>`).join("");
  select.value = preferred;
}

function modelSupportsThinking() {
  return $("#model-sel")?.selectedOptions?.[0]?.dataset.thinkingToggle === "1";
}

function syncThinkingControl() {
  const control = $("#thinking-control");
  const input = $("#thinking-sel");
  if (!control || !input) return;
  const supported = modelSupportsThinking();
  control.hidden = !supported;
  input.disabled = !supported;
  const saved = localStorage.getItem("studio_thinking");
  setThinkingEnabled(
    supported && (saved === null ? true : saved === "1"),
    { remember: false },
  );
  syncEffortControl();
}

function setThinkingEnabled(enabled, { remember = true } = {}) {
  const input = $("#thinking-sel");
  const trigger = $("#thinking-toggle");
  if (!input || !trigger) return;
  input.checked = !!enabled;
  trigger.setAttribute("aria-checked", enabled ? "true" : "false");
  if (remember) localStorage.setItem("studio_thinking", enabled ? "1" : "0");
  syncEffortControl();
  syncBatchEffortControl();
}

async function send() {
  const q = $("#q").value.trim();
  if (!q) { $("#q").focus(); return; }
  if (!$("#model-sel").value) {
    await setModelDialog(true);
    return;
  }
  if (fontFiles.length && !$("#font-license-ack")?.checked) {
    alert("请先确认拥有所上传字体用于演示与嵌入交付的权利");
    setFontPanel(true);
    return;
  }
  if (!requireAuthentication({ resume: true })) return;
  void guideOptionalMaterialServices();
  beginComposerLaunch(q);
  if (creationMode === "dynamic") {
    return sendSensePresentDazzle(q);
  }
  const slideCount = currentSlideCount();
  const body = new FormData();        // FormData:同时带表单字段 + 附件(multipart);FastAPI Form/File 都收
  body.set("query", q);
  body.set("lang", $("#deck-language-sel").value);
  body.set("model", $("#model-sel").value);
  body.set("pipeline", $("#pipeline-sel").value);
  body.set("skill", $("#skill-sel").value);
  body.set("ppt_output", "static_html");
  body.set("slide_count", slideCount);   // 0 = 自由发挥;>0 = 目标页数(上限 18)
  body.set("theme", $("#theme-sel") ? $("#theme-sel").value : "");
  body.set("style", $("#style-sel") ? $("#style-sel").value : "");
  body.set("scheme", $("#scheme-sel") ? $("#scheme-sel").value : "");   // 主题/风格/色系 = 生成偏好(空=自动)
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  body.set("effort", selectedEffort());
  body.set("font_roles", JSON.stringify(fontRoleConfig()));
  body.set("font_license_ack", $("#font-license-ack")?.checked ? "1" : "0");
  if (pipelineHasCap("attachments")) attachFiles.forEach((f) => body.append("files", f));  // 仅支持附件的管线才带
  if (pipelineHasCap("custom_fonts")) fontFiles.forEach((f) => body.append("font_files", f));
  $("#send").disabled = true;
  try {
    const r = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks`, { method: "POST", body });
    if (r.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    if (!r.ok) {
      let msg = await r.text();
      try { msg = JSON.parse(msg).detail || msg; } catch {}
      throw new Error(msg);
    }
    const j = await r.json();
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    clearAttachFiles();
    clearFontFiles();
    await openDeck(j.deck_id);          // 输入台平滑收拢到 00 会话,右侧过程栏全程直播
    refreshDeckListWhenIdle(j.deck_id);
  } catch (e) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + e.message);
  }
  finally { $("#send").disabled = false; }
}

async function sendSensePresentDazzle(query) {
  const button = $("#send");
  const body = new FormData();
  body.set("query", query);
  body.set("lang", $("#deck-language-sel").value);
  body.set("model", $("#model-sel").value);
  body.set("skill", "sense-present-dazzle");
  body.set("ppt_output", "dynamic_html");
  body.set("slide_count", currentSlideCount());
  body.set("theme", $("#theme-sel")?.value || "");
  body.set("style", $("#style-sel")?.value || "");
  body.set("scheme", $("#scheme-sel")?.value || "");
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  body.set("effort", selectedEffort());
  button.disabled = true;
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks`, { method: "POST", body });
    if (response.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "动态生成提交失败");
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    clearAttachFiles();
    await openDeck(payload.deck_id);
    refreshDeckListWhenIdle(payload.deck_id);
  } catch (error) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + error.message);
  } finally {
    button.disabled = false;
  }
}

async function sendDynamic(query) {
  const button = $("#send");
  const model = $("#model-sel").value;
  if (!model) { clearComposerLaunchMorph({ restore: true }); alert("当前没有可用的动态模型"); return; }
  button.disabled = true;
  try {
    const r = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/send`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: query,
        model,
        skill: "dazzle-deck",
        slide_count: currentSlideCount(),
        theme: $("#theme-sel")?.value || "",
        style: $("#style-sel")?.value || "",
        scheme: $("#scheme-sel")?.value || "",
        thinking: thinkingEnabled(),
      }),
    });
    if (r.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || "动态生成提交失败");
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    await openDynamic(j.conv_id, { subscribeNow: true });
    refreshDeckListWhenIdle(j.conv_id);
  } catch (e) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + e.message);
  }
  finally { button.disabled = false; }
}

function currentSlideCount() {
  const lv = $("#length-sel") ? $("#length-sel").value : "0";
  return lv === "custom"
    ? Math.min(18, Math.max(0, parseInt($("#length-num").value) || 0))
    : (parseInt(lv) || 0);
}

/* ---------------- 批量生成 ---------------- */
const BATCH_STATUS_LABEL = {
  queued: "排队中", running: "生成中", completed: "全部完成",
  partial: "部分完成", failed: "全部失败",
};
let selectedBatchId = null;
let batchTimer = null;

function batchStatusText(batch) {
  return BATCH_STATUS_LABEL[batch.status] || batch.status;
}

async function loadBatches(selectId = selectedBatchId) {
  const box = $("#batch-history");
  try {
    const { batches } = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/batches`);
    if (!batches.length) {
      box.innerHTML = '<div class="muted small">还没有批次</div>';
      $("#batch-detail").hidden = true;
      return;
    }
    box.innerHTML = batches.map((batch) => `
      <button type="button" class="batch-history-item ${batch.id === selectId ? "active" : ""}" data-id="${batch.id}">
        <span><strong>${escapeHtml(batch.name || `批次 ${batch.id}`)}</strong>
          <small>${escapeHtml(batch.model_label || batch.model)} · ${batch.total} 条</small></span>
        <em class="s-${batch.status}">${batchStatusText(batch)}</em>
      </button>`).join("");
    $$(".batch-history-item", box).forEach((button) => {
      button.onclick = () => selectBatch(+button.dataset.id);
    });
    const target = selectId || batches[0].id;
    if (target) await selectBatch(target);
  } catch (e) {
    box.innerHTML = `<div class="batch-error">批次加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderBatchDetail(batch) {
  selectedBatchId = batch.id;
  $$(".batch-history-item").forEach((item) =>
    item.classList.toggle("active", +item.dataset.id === batch.id));
  $("#batch-detail").hidden = false;
  $("#batch-detail-name").textContent = batch.name || `批次 ${batch.id}`;
  $("#batch-detail-meta").textContent =
    `${batch.model_label || modelLabel(batch.model)} · ${SKILL_LABEL[batch.skill_version] || batch.skill_version} · ${batch.total} 条`;
  const c = batch.counts;
  const finished = c.completed + c.failed;
  $("#batch-meter-bar").style.width = `${batch.total ? Math.round(finished / batch.total * 100) : 0}%`;
  $("#batch-counts").innerHTML =
    `<span>完成 <b>${c.completed}</b></span><span>生成中 <b>${c.running}</b></span>` +
    `<span>排队 <b>${c.queued}</b></span><span>失败 <b>${c.failed}</b></span>`;
  $("#batch-decks").innerHTML = (batch.decks || []).map((deck) => `
    <button type="button" class="batch-deck" data-id="${deck.id}">
      <span class="batch-deck-index">${String(deck.batch_index || 0).padStart(2, "0")}</span>
      <span class="batch-deck-title">${escapeHtml(deck.title || "未命名")}</span>
      <span class="batch-deck-status s-${deck.status}">${SKILL_LABEL[deck.skill_version] || deck.skill_version || ""} · ${STATUS_LABEL[deck.status] || deck.status}</span>
    </button>`).join("");
  $$(".batch-deck", $("#batch-decks")).forEach((button) => {
    button.onclick = () => {
      closeBatchModal();
      openDeck(+button.dataset.id);
    };
  });
  const download = $("#batch-download");
  download.hidden = !batch.download_ready;
  download.href = batch.download_ready ? `${globalThis.STUDIO_BASE_PATH || ""}/api/batches/${batch.id}/download` : "#";
}

async function selectBatch(id) {
  if (batchTimer) { clearTimeout(batchTimer); batchTimer = null; }
  selectedBatchId = id;
  try {
    const batch = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/batches/${id}`);
    renderBatchDetail(batch);
    if (!batch.terminal && !$("#batch-modal").hidden) {
      batchTimer = setTimeout(() => selectBatch(id), 3000);
    }
  } catch (e) {
    $("#batch-error").hidden = false;
    $("#batch-error").textContent = "批次详情加载失败：" + e.message;
  }
}

async function openBatchModal() {
  if (!requireAuthentication()) return;
  if (!$("#model-sel").value) {
    await setModelDialog(true);
    return;
  }
  $("#batch-error").hidden = true;
  const mainModel = $("#model-sel").value;
  if ($(`#batch-model option[value="${mainModel}"]`)) $("#batch-model").value = mainModel;
  syncBatchEffortControl();
  const mainSkill = $("#skill-sel").value;
  if ($(`#batch-skill option[value="${mainSkill}"]`)) $("#batch-skill").value = mainSkill;
  $("#batch-modal").hidden = false;
  await loadBatches();
}

function syncBatchEffortControl() {
  const option = $("#batch-model")?.selectedOptions?.[0];
  const control = $("#batch-effort-control");
  const select = $("#batch-effort");
  if (!option || !control || !select) return;
  const levels = selectableEffortLevels(option);
  control.hidden = levels.length === 0;
  select.disabled = levels.length === 0;
  if (!levels.length) {
    select.replaceChildren();
    return;
  }
  const saved = localStorage.getItem(`studio_effort_${option.value}`);
  const preferred = levels.includes(saved)
    ? saved
    : (levels.includes(option.dataset.effortDefault) ? option.dataset.effortDefault : levels[0]);
  select.innerHTML = levels.map((level) => `<option value="${level}">${level}</option>`).join("");
  select.value = preferred;
}

function closeBatchModal() {
  $("#batch-modal").hidden = true;
  if (batchTimer) { clearTimeout(batchTimer); batchTimer = null; }
}

async function submitBatch(event) {
  event.preventDefault();
  if (!$("#batch-model").value) {
    await setModelDialog(true);
    return;
  }
  const file = $("#batch-file").files[0];
  if (!file) { $("#batch-file").focus(); return; }
  const body = new FormData($("#batch-form"));
  body.set("model", $("#batch-model").value);
  body.set("skill", $("#batch-skill").value);
  body.set("slide_count", currentSlideCount());
  body.set("theme", $("#theme-sel")?.value || "");
  body.set("style", $("#style-sel")?.value || "");
  body.set("scheme", $("#scheme-sel")?.value || "");
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  body.set("effort", $("#batch-effort-control")?.hidden ? "" : ($("#batch-effort")?.value || ""));
  const button = $("#batch-submit");
  button.disabled = true;
  button.textContent = "正在创建…";
  $("#batch-error").hidden = true;
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/batches`, { method: "POST", body });
    if (!response.ok) {
      let message = await response.text();
      try { message = JSON.parse(message).detail || message; } catch {}
      throw new Error(message);
    }
    const batch = await response.json();
    $("#batch-form").reset();
    renderBatchDetail(batch);
    await loadDecks();
    await loadBatches(batch.id);
  } catch (e) {
    $("#batch-error").hidden = false;
    $("#batch-error").textContent = "提交失败：" + e.message;
  } finally {
    button.disabled = false;
    button.textContent = "开始批量生成";
  }
}

/* ---------------- 编辑器 ---------------- */
function closeEditor({ showComposerView = true } = {}) {
  ed.deckLoadController?.abort();
  ed.deckLoadController = null;
  const messages = $("#workflow-message-list");
  if (messages) { messages.replaceChildren(); delete messages.dataset.renderKey; }
  setProcRailOpen(false);
  if (ed.sse) { ed.sse.close(); ed.sse = null; }
  if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
  if (ed.dynamicPollTimer) { clearTimeout(ed.dynamicPollTimer); ed.dynamicPollTimer = null; }
  cancelWorkflowAgentTraceRequest();
  stopLive();
  ed.id = null;
  const context = $("#case-context");
  if (context) context.hidden = true;
  const modelBadge = $("#case-model-badge");
  if (modelBadge) modelBadge.hidden = true;
  const queryDetails = $("#case-query");
  if (queryDetails) queryDetails.open = false;
  const queryCopy = $("#case-query-copy");
  if (queryCopy) {
    clearTimeout(queryCopy._labelTimer);
    if (queryCopy._fallbackHtml) queryCopy.innerHTML = queryCopy._fallbackHtml;
    queryCopy.disabled = true;
  }
  ed.workflowToolOutputs = {};
  const frame = $("#canvas-deck");
  frame.hidden = true; frame.src = "about:blank";
  delete frame.dataset.deckKind;
  delete frame.dataset.deckId;
  delete frame.dataset.pendingSlide;
  if (location.hash.match(/^#(?:static|dynamic)-/)) history.replaceState(null, "", location.pathname + location.search);
  if (showComposerView) showComposer();
}

function renderDeckLoadState(failed = false) {
  const empty = $("#canvas-empty");
  empty.hidden = false;
  empty.innerHTML = `<div class="workstream-head deck-load-state" role="status">
    ${muralMakingMarkup({rows: [], total: 0, previews: 0, active: !failed, loadingRecords: true,
      status: failed ? "任务记录读取失败" : "正在读取任务记录"}, true)}
    ${failed ? '<p>连接失败或读取超过 20 秒。这不代表生成失败；可以重试或返回列表。</p>' : ""}
    ${failed ? '<button type="button" data-deck-load-retry>重新读取</button>' : ""}</div>`;
  const retry = empty.querySelector("[data-deck-load-retry]");
  if (retry) retry.onclick = () => openDeck(ed.id);
  syncViewToggle();
}

async function openDeck(id) {
  clearStudioNotices();
  closeEditor({ showComposerView: false });
  history.replaceState(null, "", `#static-${encodeURIComponent(id)}`);
  $$("#deck-list .deck-item").forEach((a) => a.classList.toggle("active", a.dataset.id == id));
  stopLive();
  Object.assign(ed, { id, kind: "static", total: 0, authored: new Set(), rendered: new Set(), sel: null, follow: true, status: null,
                      lastPhase: null, feedSeen: new Set(), finalized: false, planCache: {},
                      ppCollapsed: true,
                      feed: {}, pageAgents: {}, workflow: {}, muralRuntime: {}, liveEventCursor: 0, liveEventSchema: "", liveFeedLoaded: false, liveFeedRequest: null, liveToolDetails: new Map(), workspaceView: "workflow", viewMode: "ppt",
                      workflowPane: "page", workflowPage: 1, workflowNodeOffsets: {}, workflowPanelCollapsed: { files: false, conversation: false }, workflowArtifactRel: "", workflowArtifactFallback: null,
                      workflowSplitRatio: workflowSplitRatio(localStorage.getItem("studio_workflow_split_ratio")),
                      workflowOwnerAgent: "", workflowOwnerTask: "", workflowSelectedNodeId: "",
                      workflowConversationMode: "history", workflowTraceAgent: "", workflowTraceSegment: "", workflowTraceSource: "messages", workflowTracePayload: null, workflowTraceLoading: false, workflowTraceRequest: 0, workflowTraceAbortController: null, workflowTraceExpanded: false, workflowTraceLoadedAt: 0,
                      workflowArtifactCache: {}, workflowArtifactLoading: "", workflowArtifactRequest: 0,
                      workflowDocuments: {}, workflowDocumentSignature: "", workflowDocumentLoading: "",
                      specialistArtifacts: {}, agentTimings: {}, overallTiming: null,
                      staticDeckUrl: trajectoryMode ? trajectoryDeckApi(id, "/files/present.html") : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${id}/files/present.html`, staticDeckReady: false,
                      staticDeckFinal: false, staticHtmlStamp: 0, staticStamp: Date.now(),
                      fullQuery: "",
                      briefMeta: {}, attachments: [], staticFollowups: [], staticConversationTurns: [],
                      resumeEligible: false, resumeExitReason: null,
                      conKey: null, conLines: [],
                      atimes: {}, rtimes: {}, staticDirtyPages: new Set(), phLines: {}, historyRequest: 0,
                      pageHistoryCache: new Map(), pageHistoryPending: new Map(),
                      canvasSpec: null,
                      historyScopePage: null, historyRenderKey: "", speechRenderKey: "",
                      speechScopePage: null, speechRequest: 0, speechPages: new Map(), hasAnySpeech: false, stopRequested: false });
  $("#editor").dataset.kind = "static";
  setCanvasSpec(ed.canvasSpec);
  setRailLabels("static");
  $("#plan-panel").hidden = true;
  $("#pr-feed").innerHTML = "";
  $("#pr-act").textContent = "";
  $("#pr-count").textContent = "–";
  $("#pr-bar").style.width = "0%";
  $$("#pr-steps li").forEach((li) => (li.className = ""));
  buildFilmstrip();
  $("#canvas-img").hidden = true;
  $("#canvas-deck").hidden = true;
  renderDeckLoadState();
  $("#follow-btn").classList.add("on");
  const controller = new AbortController();
  ed.deckLoadController = controller;
  const timeout = setTimeout(() => controller.abort(), 20000);
  const isCurrent = () => ed.deckLoadController === controller && ed.id === id;
  const progressPromise = jget(trajectoryMode ? trajectoryDeckApi(id, "/progress") : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${id}/progress`, {signal: controller.signal});
  const workspaceTransition = transitionComposerToEditor();
  try {
    const p = await progressPromise;
    clearTimeout(timeout);
    if (!isCurrent()) return;
    if (p.status === "completed") await workspaceTransition;
    if (!isCurrent()) return;
    $("#ed-title").textContent = p.title || "演示文稿";
    apply(p);
    if (ed.status === "completed" && ed.staticDeckReady && ed.workspaceView !== "ppt") setWorkspaceView("ppt");
    setTopbar(
      p.title || "演示文稿",
      `${editorPresentationKind() === "dynamic" ? "动态" : "静态"}演示 · AI 生成内容请注意核验`,
    );
    if (isStaticActiveStatus(p.status)) {
      if (trajectoryMode) {
        ed.timer = setInterval(async () => {
          if (ed.id !== id) return;
          try { apply(await jget(trajectoryDeckApi(id, "/progress"))); } catch {}
        }, 2000);
      } else {
        subscribe(id);
        ed.timer = setInterval(tick, 1000);
      }
      startLive();
    }
    await workspaceTransition;
    if (isCurrent()) finishComposerLaunchMorph();
  } catch {
    if (!isCurrent()) return;
    await workspaceTransition;
    if (!isCurrent()) return;
    renderDeckLoadState(true);
    finishComposerLaunchMorph();
  } finally {
    clearTimeout(timeout);
    if (isCurrent()) ed.deckLoadController = null;
  }
}

/* ---------------- 动态演示：复用同一套编辑器外壳 ---------------- */
function setRailLabels(kind) {
  const labels = kind === "dynamic"
    ? [["理解与规划", "明确叙事结构与动态策略"], ["素材与风格", "准备事实、图像与视觉方向"], ["页面制作", "逐页编排布局、动效与交互"], ["质量检查", "渲染复审与全局一致性检查"], ["完成", ""]]
    : [["理解与规划", "明确叙事结构与页面目标"], ["素材与风格", "准备事实、图像与视觉方向"], ["页面制作", "逐页完成内容、排版与渲染"], ["质量检查", "成稿复审与全局一致性检查"], ["完成", ""]];
  $$("#pr-steps li").forEach((li, i) => {
    li.querySelector("b").textContent = labels[i][0];
    li.querySelector("i").textContent = labels[i][1];
  });
  $("#pr-countlabel").textContent = "页面已完成";
  const runtime = $("#pr-runtime");
  if (runtime) {
    runtime.dataset.kind = kind;
    runtime.querySelector("b").textContent = kind === "dynamic" ? "连续动态创作" : "多页面同步制作";
    runtime.querySelector("i").textContent = kind === "dynamic"
      ? "从整体规划到逐页动效，由同一创作流程持续推进"
      : "完成整体规划后，多页内容与视觉会同步推进";
  }
}

async function openDynamic(convId, { subscribeNow = false } = {}) {
  clearStudioNotices();
  closeEditor({ showComposerView: false });
  history.replaceState(null, "", `#dynamic-${encodeURIComponent(convId)}`);
  $$("#deck-list .deck-item").forEach((a) => a.classList.toggle("active", a.dataset.kind === "dynamic" && a.dataset.id === convId));
  Object.assign(ed, {
    id: convId, kind: "dynamic", total: 0, rendered: new Set(), sel: null, follow: true,
    status: "running", fullQuery: "", dynamicDeckUrl: "", dynamicLogs: [], dynamicSeq: new Set(),
    dynamicPhase: "plan", dynamicStamp: Date.now(), dynamicStartedAt: Date.now() / 1000,
    elapsedBase: 0, elapsedAt: Date.now(), planCache: {}, ppCollapsed: true,
    workspaceView: "process", viewMode: "ppt", workflow: {}, muralRuntime: {},
    workflowPane: "graph", workflowPage: 1, workflowNodeOffsets: {}, workflowPanelCollapsed: { files: false, conversation: false }, workflowArtifactRel: "", workflowArtifactFallback: null,
    workflowOwnerAgent: "", workflowOwnerTask: "", workflowSelectedNodeId: "",
    workflowConversationMode: "history", workflowTraceAgent: "", workflowTraceSegment: "", workflowTraceSource: "messages", workflowTracePayload: null, workflowTraceLoading: false, workflowTraceRequest: 0, workflowTraceAbortController: null, workflowTraceExpanded: false, workflowTraceLoadedAt: 0,
    workflowArtifactCache: {}, workflowArtifactLoading: "", workflowArtifactRequest: 0,
    workflowDocuments: {}, workflowDocumentSignature: "", workflowDocumentLoading: "",
    dynamicModelLabel: "动态模型", dynamicFeedSeen: new Set(), dynamicError: "",
    briefMeta: {}, attachments: [], pageAgents: {}, dynamicFollowups: [], dynamicChatTurns: [],
    agentTimings: {}, overallTiming: null,
    speechRenderKey: "", speechScopePage: null, speechRequest: 0, speechPages: new Map(), hasAnySpeech: false, stopRequested: false,
    canvasSpec: null,
  });
  setCanvasSpec(ed.canvasSpec);
  $("#editor").dataset.kind = "dynamic";
  setRailLabels("dynamic");
  $("#plan-panel").hidden = true;
  $("#pr-feed").innerHTML = ""; $("#pr-act").textContent = "正在启动动态 Agent…"; $("#pr-elapsed").textContent = "";
  $("#pr-count").textContent = "–"; $("#pr-bar").style.width = "4%";
  $$("#pr-steps li").forEach((li) => (li.className = ""));
  $("#canvas-img").hidden = true; $("#canvas-deck").hidden = true;
  buildFilmstrip(); select(0, { byUser: false });
  const conversationPromise = jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/conversation?conv_id=${encodeURIComponent(convId)}`);
  const workspaceTransition = transitionComposerToEditor();
  let data;
  try { data = await conversationPromise; }
  catch (e) {
    await workspaceTransition;
    showStudioNotice(`演示加载失败：${e.message}`, { type: "error" });
    finishComposerLaunchMorph();
    return;
  }
  $("#ed-title").textContent = data.meta?.title || "动态演示";
  setTopbar(data.meta?.title || "动态演示", "动态演示 · AI 生成内容请注意核验");
  ed.dynamicModelLabel = data.meta?.model_label || data.meta?.model_key || "动态模型";
  ed.briefMeta = { ...(data.meta || {}), skill_version: "dazzle-deck", thinking: data.meta?.thinking === true };
  if (data.meta?.user_query) ed.fullQuery = data.meta.user_query;
  const events = data.events || [];
  const eventTimes = events.map((event) => Number(event.ts)).filter(Number.isFinite);
  if (eventTimes.length) ed.dynamicStartedAt = eventTimes[0];
  const terminalEvent = [...events].reverse().find((event) => event.kind === "done" || event.kind === "error");
  const dynamicEndedAt = data.active
    ? Date.now() / 1000
    : Number(terminalEvent?.ts || eventTimes.at(-1) || ed.dynamicStartedAt);
  ed.elapsedBase = Math.max(0, Math.round(dynamicEndedAt - ed.dynamicStartedAt));
  ed.elapsedAt = Date.now();
  ed.dynamicHydrating = true;
  try {
    events.forEach((event) => applyDynamicEvent(event, { historical: true }));
  } finally {
    ed.dynamicHydrating = false;
  }
  syncOutlineBrief();
  syncOutlineConversationTurns();
  if (!data.active && ed.status === "running") ed.status = data.meta?.status || "interrupted";
  if (data.active || subscribeNow) {
    ed.status = "running"; subscribeDynamic(convId); ed.timer = setInterval(tick, 1000);
  }
  renderDynamicChrome();
  renderDynamicConsole();
  if (ed.status === "completed" && ed.dynamicDeckUrl) setWorkspaceView("ppt");
  await workspaceTransition;
  finishComposerLaunchMorph();
}

function dynamicLog(text, kind = "text", timestamp = null) {
  if (!text) return;
  const entry = { text: String(text).replace(/\s+/g, " ").trim().slice(0, 260), kind, created_at: timestamp };
  ed.dynamicLogs.push(entry);
  ed.dynamicChatTurns ||= [];
  if (kind === "user") {
    ed.dynamicChatTurns.push({ role: "user", content: entry.text, created_at: timestamp });
    ed.dynamicChatTurns.push({ role: "assistant", logs: [], current: true, status: "running", created_at: timestamp });
    ed.dynamicChatTurns.slice(0, -1).forEach((turn) => {
      if (turn.role === "assistant") turn.current = false;
    });
  } else {
    let assistant = [...ed.dynamicChatTurns].reverse().find((turn) => turn.role === "assistant");
    if (!assistant) {
      assistant = { role: "assistant", logs: [], current: true, status: "running", created_at: timestamp };
      ed.dynamicChatTurns.push(assistant);
    }
    assistant.logs.push(entry);
  }
  if (ed.kind === "dynamic" && !ed.dynamicHydrating) {
    if (kind === "user") syncOutlineConversationTurns();
    renderDynamicConsole();
  }
}

function dynamicPhaseForTool(name, args = {}) {
  if (["read_file"].includes(name)) return "plan";
  if (["image_generate", "web_search", "web_fetch"].includes(name)) return "research";
  if (["inspect", "inspect_group", "vision_analyze"].includes(name) || (name === "bash" && /--all/.test(args.command || ""))) return "verify";
  return "render";
}

function requestedSlideCount(text) {
  const value = String(text || "");
  const preferred = value.match(/目标篇幅[：:]\s*(?:约\s*)?(\d+)\s*页/);
  const natural = value.match(/(?:做|制作|生成|需要|共|约|一套)?\s*(\d+)\s*页(?:的|PPT|ppt|演示|幻灯片)?/);
  const count = Number(preferred?.[1] || natural?.[1] || 0);
  return Number.isInteger(count) && count > 0 && count <= 40 ? count : 0;
}

function advanceDynamicPhase(nextPhase) {
  if (nextPhase === "done") { ed.dynamicPhase = "done"; return; }
  const current = STEP_ORDER.indexOf(ed.dynamicPhase);
  const next = STEP_ORDER.indexOf(nextPhase);
  if (next >= 0 && next >= current) ed.dynamicPhase = nextPhase;
}

function applyDynamicEvent(event, { historical = false } = {}) {
  if (ed.kind !== "dynamic" || (event.conv_id && event.conv_id !== ed.id)) return;
  if (event.seq && ed.dynamicSeq.has(event.seq)) return;
  if (event.seq) ed.dynamicSeq.add(event.seq);
  const k = event.kind;
  if (k === "user") {
    ed.fullQuery = ed.briefMeta?.user_query || event.text || ed.fullQuery;
    const requested = requestedSlideCount(event.text);
    if (requested && !ed.total) { ed.total = requested; buildFilmstrip(); }
    if (event.first !== false) {
      $("#ed-title").textContent = (event.text || "动态演示").slice(0, 56);
      setTopbar((event.text || "动态演示").slice(0, 56), "动态演示 · AI 生成内容请注意核验");
    } else if (event.text && !ed.dynamicFollowups?.includes(event.text)) {
      ed.dynamicFollowups ||= [];
      ed.dynamicFollowups.push(event.text);
    }
    dynamicLog(event.text, "user", event.ts);
    syncOutlineBrief();
    syncOutlineConversationTurns();
  } else if (k === "assistant_text") {
    dynamicLog(event.text, "text", event.ts);
  } else if (k === "tool_call") {
    advanceDynamicPhase(dynamicPhaseForTool(event.name, event.args || {}));
    const action = event.label || TOOL_LABEL[event.name] || "推进制作任务";
    const source = event.args?.path || event.args?.prompt || "";
    const detail = source ? compactActivityHint(source) : "";
    dynamicLog(`${action}${detail ? " · " + detail : ""}`, "tool", event.ts);
    if (!historical) feedAdd(`<span class="pr-txt">${escapeHtml(action)}</span>`);
  } else if (k === "tool_result") {
    if (!event.ok && !historical) feedAdd(`<span class="pr-txt pr-bad">${escapeHtml(event.label || event.name)}未通过，正在自动调整</span>`);
    dynamicLog(event.ok ? "当前步骤已完成" : "发现问题，正在自动修正", event.ok ? "result" : "error", event.ts);
  } else if (k === "vision") {
    advanceDynamicPhase("verify"); dynamicLog(`检查画面 · ${event.path || "渲染图"}`, "tool", event.ts);
  } else if (k === "deck_update" || k === "page_rendered") {
    if (event.deck_url) ed.dynamicDeckUrl = event.deck_url;
    if (event.n_pages) ed.total = Math.max(ed.total, +event.n_pages || 0);
    if (event.slide) ed.rendered.add(+event.slide);
    else if (k === "page_rendered" && event.n_pages) for (let i = 1; i <= event.n_pages; i++) ed.rendered.add(i);
    ed.dynamicStamp = Date.now(); advanceDynamicPhase(k === "page_rendered" ? "verify" : "render");
    buildFilmstrip();
    if (event.slide && ed.follow && ed.workspaceView === "ppt" && ed.viewMode !== "console") select(+event.slide, { byUser: false });
    if (k === "page_rendered") {
      const pages = event.slide
        ? [+event.slide]
        : (event.n_pages ? Array.from({ length: +event.n_pages }, (_, index) => index + 1) : []);
      pages.forEach((n) => {
        const key = `page:${n}`;
        if (ed.dynamicFeedSeen.has(key)) return;
        ed.dynamicFeedSeen.add(key);
        feedAdd(`<img class="pr-thumb" src="${fileUrl(n)}" alt=""><span class="pr-txt">第 ${n} 页渲染完成</span>`, "slide", event.ts);
      });
    }
  } else if (k === "note") {
    dynamicLog(event.text, "note", event.ts);
  } else if (k === "error") {
    dynamicLog(event.message, "error", event.ts);
    ed.dynamicError = event.message || "动态生成失败";
    ed.status = "error";
    const assistant = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
    if (assistant) { assistant.status = "error"; assistant.responded_at = event.ts; }
    notifyRunFailure(ed.dynamicError, "动态演示生成失败");
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
  } else if (k === "final") {
    dynamicLog(event.text, "final", event.ts);
  } else if (k === "done") {
    const eventStatus = event.status === "completed" ? "completed" : (event.status || "failed");
    ed.status = ed.stopRequested ? "stopped" : eventStatus;
    if (ed.status === "completed") {
      advanceDynamicPhase("done");
      for (let n = 1; n <= ed.total; n++) ed.rendered.add(n);
      buildFilmstrip();
    }
    const endedAt = Number(event.ts);
    if (Number.isFinite(endedAt)) {
      ed.elapsedBase = Math.max(0, Math.round(endedAt - ed.dynamicStartedAt));
      ed.elapsedAt = Date.now();
    }
    if (!ed.dynamicFeedSeen.has("done")) {
      ed.dynamicFeedSeen.add("done");
      const duration = ed.elapsedBase != null ? ` · 用时 ${fmtDur(ed.elapsedBase)}` : "";
      if (ed.status === "completed")
        feedAdd(`<span class="pr-txt pr-ok">✓ 全部完成 · 共 ${ed.total || ed.rendered.size} 页${duration}</span>`, "", event.ts);
    }
    if (ed.status !== "completed") {
      const failureText = ed.dynamicError || (ed.status === "stopped" ? "生成已中断" : `生成未完成 · ${STATUS_LABEL[ed.status] || ed.status}`);
      notifyRunFailure(failureText, ed.status === "stopped" ? "生成已中断" : "动态演示生成失败");
    }
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
    const assistant = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
    if (assistant) { assistant.status = ed.status; assistant.responded_at = event.ts; }
    if (ed.status === "completed" && Number(ed.sel) > 0) {
      // speech.md is often written during final packaging, after the last page
      // render event. Probe once more; the drawer stays absent when none exists.
      setTimeout(() => loadDynamicPageSpeech(Number(ed.sel), { quiet: true }), 0);
    }
    loadDecks(ed.id);
  }
  if (!historical) renderDynamicChrome();
}

function renderDynamicChrome() {
  if (ed.kind !== "dynamic") return;
  const completed = ed.status === "completed";
  const running = ed.status === "running";
  const failed = !running && !completed;
  const phaseLabel = { plan: "理解需求", research: "风格与素材", render: "逐页编排", verify: "渲染复审", done: "完成" }[ed.dynamicPhase] || "制作中";
  const elapsedSeconds = currentElapsedSeconds();
  const elapsed = elapsedSeconds != null ? fmtDur(elapsedSeconds) : "";
  const status = $("#ed-status");
  const chromeKey = JSON.stringify([ed.id, ed.dynamicModelLabel, ed.status, ed.dynamicPhase]);
  if (status.dataset.dynamicChromeKey !== chromeKey) {
    status.dataset.dynamicChromeKey = chromeKey;
    status.innerHTML = `<span class="badge ${completed ? "ph-done" : (failed ? "ph-failed" : "")}">${completed ? "完成" : (failed ? STATUS_LABEL[ed.status] || ed.status : phaseLabel)}</span>`;
  }
  if ($("#pr-elapsed").textContent !== elapsed) $("#pr-elapsed").textContent = elapsed;
  const idx = STEP_ORDER.indexOf(ed.dynamicPhase);
  $$("#pr-steps li").forEach((li) => { const i = STEP_ORDER.indexOf(li.dataset.k); li.className = i < idx || completed ? "done" : (i === idx ? "now" : ""); });
  $("#pr-count").innerHTML = ed.total ? `${ed.rendered.size}<small>/${ed.total}</small>` : "–";
  const pct = ed.total ? Math.round(ed.rendered.size / ed.total * 100) : (completed ? 100 : 6);
  $("#pr-bar").style.width = `${completed ? 100 : pct}%`; $("#ed-bar").style.width = `${completed ? 100 : Math.max(6, pct)}%`;
  $("#pr-act").textContent = running ? (ed.dynamicLogs.at(-1)?.text || "正在准备…") : "";
  renderActions(ed.status);
  updateNav();
  syncTaskComposerState();
  syncTaskConfig();
  syncViewToggle();
}

function subscribeDynamic(convId) {
  if (ed.sse) ed.sse.close();
  const es = new EventSource(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/stream?conv_id=${encodeURIComponent(convId)}`); ed.sse = es;
  es.onmessage = (message) => {
    if (ed.kind !== "dynamic" || ed.id !== convId) { es.close(); return; }
    try { applyDynamicEvent(JSON.parse(message.data)); } catch {}
  };
  es.addEventListener("end", () => { es.close(); if (ed.sse === es) ed.sse = null; });
}

function startDynamicConversationPoll(convId) {
  if (ed.dynamicPollTimer) clearTimeout(ed.dynamicPollTimer);
  const poll = async () => {
    if (ed.kind !== "dynamic" || String(ed.id) !== String(convId)) return;
    try {
      const data = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/conversation?conv_id=${encodeURIComponent(convId)}`);
      (data.events || []).forEach((event) => applyDynamicEvent(event));
      if (!data.active) {
        if (ed.status === "running") ed.status = data.meta?.status || "interrupted";
        renderDynamicChrome();
        ed.dynamicPollTimer = null;
        return;
      }
    } catch {}
    ed.dynamicPollTimer = setTimeout(poll, 1400);
  };
  ed.dynamicPollTimer = setTimeout(poll, 320);
}

function renderDynamicConsole() {
  if (ed.kind !== "dynamic" || ed.dynamicHydrating || !$("#gen-console")) return;
  const body = $("#agent-progress-console") || $("#gen-console");
  const currentTurn = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
  const events = (currentTurn?.logs || ed.dynamicLogs).flatMap((entry) => {
    if (entry.kind === "user") return [];
    if (entry.kind === "tool") return [{ k: "tool", tool: "dynamic_action", hint: entry.text }];
    if (entry.kind === "result") return [];
    return [{ k: "text", s: entry.text }];
  });
  renderAgentProgress(body, events, {
    running: ed.status === "running",
    rendered: ed.sel > 0 && ed.rendered.has(ed.sel),
    n: ed.sel || 0,
    source: `dynamic:${ed.sel || 0}`,
  });
}

function apply(p) {
  if (ed.id == null) return;
  syncStaticTitle(ed.id, p);
  if (ed.stopRequested) p = { ...p, status: "interrupted", error: "用户已停止" };
  $("#canvas-empty > .deck-load-state")?.remove();
  ed.status = p.status;
  ed.artifactStatus = p.artifact_status || "";
  ed.trainingAdmission = p.training_admission || null;
  ed.trainingRejectionReasons = p.training_rejection_reasons || [];
  ed.resumeEligible = p.resume_eligible === true;
  ed.resumeExitReason = p.resume_exit_reason || null;
  setCanvasSpec(p.canvas_spec);
  ed.muralRuntime = p.mural_runtime || ed.muralRuntime || {};
  ed.attachmentParse = p.attachment_parse || [];
  // 已加载上下文 / Skill provenance:真实落盘的加载事实(仅安全元数据)。
  if (p.skill_provenance && typeof p.skill_provenance === "object") ed.skillProvenance = p.skill_provenance;
  // 正式 present.html 或任意单页 HTML 均可进入实时 PPT。生成期由后端
  // 即时拼装临时播放器，finalize 后同一路径无缝切换到正式文件。
  const wasStaticDeckFinal = !!ed.staticDeckFinal;
  const nextHtmlStamp = Number(p.html_stamp) || 0;
  const htmlPreviewChanged = !!nextHtmlStamp && nextHtmlStamp !== ed.staticHtmlStamp;
  ed.staticDeckReady = !!p.html_ready;
  ed.staticDeckFinal = !!p.html_final;
  ed.failedIntermediate = !!p.html_failed_intermediate;
  if (["failed", "rejected", "interrupted", "api_failed", "paused_recoverable"].includes(p.status)) {
    // A workspace may contain present.html even when generation failed. Keep
    // rendered PNGs as forensic previews, but never mount that mutable HTML as
    // though it were a completed deck.
    ed.staticDeckReady = false;
    ed.staticDeckFinal = false;
  }
  const finalPreviewPromoted = ed.staticDeckFinal && !wasStaticDeckFinal;
  ed.pptOutput = p.ppt_output || ed.pptOutput || "static_html";
  if (p.html_entry) ed.staticDeckUrl = trajectoryMode
    ? trajectoryDeckApi(ed.id, `/files/${p.html_entry}`)
    : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/files/${p.html_entry}`;
  const presentationKind = editorPresentationKind();
  $("#editor").dataset.output = presentationKind;
  setRailLabels(presentationKind);
  if (nextHtmlStamp) {
    ed.staticHtmlStamp = nextHtmlStamp;
    ed.staticStamp = nextHtmlStamp;
  }
  if (typeof p.query === "string") ed.fullQuery = p.query;
  ed.briefMeta = {
    ...(ed.briefMeta || {}), model: p.model, model_label: p.model_label,
    pipeline: p.pipeline,
    skill_version: p.skill_version, slide_count: p.slide_count,
    theme: p.theme, style: p.style, scheme: p.scheme, user_query: p.user_query,
    runtime_limits: p.runtime_limits || ed.briefMeta?.runtime_limits,
    thinking: p.thinking === true,
    parent_deck_id: p.parent_deck_id, revision_no: p.revision_no,
    revision_supported: p.revision_supported,
    verified_delivery_download: p.verified_delivery_download === true,
  };
  if (typeof p.revision_instruction === "string" && p.revision_instruction.trim()) {
    ed.staticFollowups = [p.revision_instruction.trim()];
  }
  if (Array.isArray(p.conversation_turns)) ed.staticConversationTurns = p.conversation_turns;
  if (Array.isArray(p.attachments)) ed.attachments = p.attachments;
  syncOutlineBrief();
  syncOutlineConversationTurns();
  ed.elapsedBase = p.elapsed_s ?? null; ed.elapsedAt = Date.now();

  // 总页数(计划出来前未知,先用已渲数撑起胶片条)
  const known = p.slides_total || 0;
  const authored = Array.isArray(p.authored) ? p.authored.map(Number).filter((n) => n > 0) : [];
  const maxR = p.rendered.length ? Math.max(...p.rendered) : 0;
  const maxA = authored.length ? Math.max(...authored) : 0;
  const total = Math.max(known, maxR, maxA, p.slides_authored || 0);
  if (total !== ed.total) { ed.total = total; buildFilmstrip(); }

  // HTML page completion and PNG rendering are separate milestones. Once a
  // complete slide_XX.html is published, show it in the right-side live player;
  // filmstrip thumbnails still wait for their stable PNG.
  const atimes = p.atimes || {};
  for (const n of authored) {
    const key = String(n);
    const isNew = !ed.authored.has(n);
    ed.authored.add(n);
    // A page is safe to reveal once when it first appears. Subsequent Agent
    // patches are intentionally not pushed into the live iframe one by one:
    // their successful PNG render below is the stable visual checkpoint. This
    // prevents a rapid patch/render loop from flashing the entire preview.
    if (isNew) ed.staticDirtyPages?.add(n);
    ed.atimes[key] = atimes[key] || ed.atimes[key] || 0;
    if (isNew && ed.follow && ed.workspaceView === "ppt" && ed.viewMode !== "console") {
      select(n, { byUser: false });
    }
  }

  // 新渲染好的页:替换占位 → 显影;已显示的页被重渲覆盖 → 换新图
  const rtimes = p.rtimes || {};
  for (const n of p.rendered) {
    const k = String(n);
    if (!ed.rendered.has(n)) {
      ed.rendered.add(n);
      ed.rtimes[k] = rtimes[k] || 0;
      ed.staticDirtyPages?.add(n);
      fillFrame(n);
      if (ed.follow && ed.workspaceView === "ppt" && ed.viewMode !== "console") select(n, { byUser: false });
    } else if (rtimes[k] && rtimes[k] !== ed.rtimes[k]) {
      ed.rtimes[k] = rtimes[k];
      ed.staticDirtyPages?.add(n);
      refreshFrame(n);
    }
  }

  // The formal player replaces the provisional fragment-backed player once.
  // Mark every authored page dirty so the next selected page loads the final
  // present.html, while normal production refreshes remain page-scoped.
  if (finalPreviewPromoted) authored.forEach((n) => ed.staticDirtyPages?.add(n));

  // html_stamp belongs to the whole provisional player: another page being
  // authored also changes it. Keep the selected iframe stable unless that
  // selected page itself has a new successful render. Dirty background pages
  // are picked up lazily when the user navigates to them.
  if ((htmlPreviewChanged || finalPreviewPromoted) && ed.staticDirtyPages?.has(Number(ed.sel))
      && ed.workspaceView === "ppt" && ed.viewMode !== "console"
      && ed.sel > 0 && ed.authored.has(Number(ed.sel))) {
    showStaticDeck(ed.sel);
  }

  // 顶栏状态 + 右侧过程栏 + 进度线
  const phase = p.phase || "starting";
  renderStatus(p, phase);
  railApply(p, phase);
  const pct = ed.total ? Math.round((p.slides_rendered / ed.total) * 100)
    : (p.status === "completed" ? 100 : 4);
  $("#ed-bar").style.width = (p.status === "completed" ? 100 : pct) + "%";

  // 画布空态文案:阶段 + 引擎此刻在做什么
  if (!ed.rendered.size && $("#canvas-empty .ce-text") && !$(".workstream-head", $("#canvas-empty"))) {
    const act = actText(p);
    $("#canvas-empty .ce-text").textContent =
      p.status === "not_started" ? "尚未开始生成"
        : p.status === "waiting" ? "修订已排队，等待上一版完成…"
        : p.status === "queued" ? "排队中…"
        : (PHASE_LABEL[phase] || "筹备中") + "…" + (act ? `  ${act}` : "");
  }

  // 选中页的规划 md 可能刚写出来 → 补拉(含 00=deck.md)
  if (ed.sel != null && !ed.planCache[ed.sel]) loadPlan(ed.sel);

  // 完成任务直接准备成稿，历史轨迹在进入 Workflow 后再读取。
  if (ed.sel == null) {
    if (p.status === "completed" && ed.staticDeckReady) {
      ed.workspaceView = "ppt";
      $("#canvas-empty").replaceChildren();
      select(ed.authored.size ? Math.min(...ed.authored) : 1, { byUser: false });
    } else select(0, { byUser: false });
  }

  // 终态
  if (["completed", "failed", "rejected", "interrupted", "paused_recoverable"].includes(p.status)) {
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
    stopLive();
    if (p.status !== "completed") {
      notifyRunFailure(
        p.error,
        p.status === "paused_recoverable"
          ? "上游通道中断，安全检查点已保留"
          : p.status === "interrupted"
          ? "生成已中断"
          : (p.status === "rejected" ? "成稿校验未通过" : "静态演示生成失败"),
        p.error_kind,
      );
      if (!ed.rendered.size && $("#canvas-empty .ce-text")) $("#canvas-empty .ce-text").textContent = "未能生成成稿";
      if (ed.sel > 0 && ed.rendered.has(Number(ed.sel)) && ed.viewMode !== "console") {
        keepStaticPng(Number(ed.sel));
      }
      const ring = $(".develop-ring");           // 终态视图可能没有加载环
      if (ring) ring.style.display = "none";
    }
    if (p.status === "completed" && ed.workspaceView === "ppt" && ed.sel > 0 && ed.viewMode !== "console") {
      select(ed.sel, { byUser: false });
    }
    loadDecks(ed.id);
  }
  renderActions(p.status);
  updateNav();
  syncTaskConfig();
  syncViewToggle();
}

function taskConfigData() {
  const model = ed.kind === "dynamic"
    ? (ed.dynamicModelLabel || "动态模型")
    : (ed.briefMeta?.model_label || (ed.briefMeta?.model ? modelLabel(ed.briefMeta.model) : "—"));
  const skillKey = ed.kind === "dynamic" ? "dazzle-deck" : ed.briefMeta?.skill_version;
  const skill = SKILL_LABEL[skillKey] || (skillKey === "dazzle-deck" ? "Dazzle Deck" : skillKey) || "—";
  const preferences = [ed.briefMeta?.slide_count ? `约 ${ed.briefMeta.slide_count} 页` : "", ed.briefMeta?.theme,
    ed.briefMeta?.style, ed.briefMeta?.scheme].filter(Boolean).join(" · ");
  const thinking = ed.briefMeta?.thinking === true ? "开启" : "关闭";
  const limits = ed.briefMeta?.runtime_limits || {};
  const maxTokens = Number(limits.max_tokens) || 40960;
  const configuredMain = ed.kind === "dynamic"
    ? Number(limits.dynamic_max_turns)
    : Number(limits.static_max_turns);
  const mainTurns = configuredMain > 0 ? configuredMain : (ed.kind === "dynamic" ? 4096 : 240);
  const childRaw = Number(limits.static_subagent_max_turns);
  let runtime;
  if (ed.kind === "dynamic") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (childRaw > 0) {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 子 Agent ${childRaw.toLocaleString()} 轮`;
  } else if (skillKey === "sn-ppt-web" || skillKey === "mural-presenter" || skillKey === "mural-presenter-creative") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 页面子 Agent 36–120 轮 · 其他子 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (["visual-craft", "visual-craft-v3-pre-speech", "visual-craft-v3-speech"].includes(skillKey)) {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 页面子 Agent 28 轮 · 其他子 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (skillKey === "sense-present-standard") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent 240 轮 · 子 Agent 240 轮`;
  } else {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 子 Agent ${mainTurns.toLocaleString()} 轮`;
  }
  return { model, skill, thinking, preferences, runtime };
}

function syncTaskConfig() {
  const { model, skill, thinking, preferences, runtime } = taskConfigData();
  const summaryNode = $("#pr-config-summary");
  const modelNode = $("#pr-config-model");
  const skillNode = $("#pr-config-skill");
  const thinkingNode = $("#pr-config-thinking");
  const runtimeNode = $("#pr-config-runtime");
  const prefNode = $("#pr-config-pref");
  const prefRow = $("#pr-config-pref-row");
  if (summaryNode) {
    summaryNode.textContent = `深度思考：${thinking === "开启" ? "已开启" : "未开启"}`;
    summaryNode.dataset.enabled = thinking === "开启" ? "1" : "0";
  }
  if (modelNode) modelNode.textContent = model;
  if (skillNode) skillNode.textContent = skill;
  if (thinkingNode) thinkingNode.textContent = thinking;
  if (runtimeNode) runtimeNode.textContent = runtime;
  if (prefNode) prefNode.textContent = preferences || "—";
  if (prefRow) prefRow.hidden = !preferences;
  const outlineModel = $("#outline-config-model");
  const outlineSkill = $("#outline-config-skill");
  const outlineThinking = $("#outline-config-thinking");
  const outlineRuntime = $("#outline-config-runtime");
  const outlinePref = $("#outline-config-pref");
  const outlinePrefRow = $("#outline-config-pref-row");
  const outlineSummary = $("#outline-config-summary");
  if (outlineModel) outlineModel.textContent = model;
  if (outlineSkill) outlineSkill.textContent = skill;
  if (outlineThinking) outlineThinking.textContent = thinking;
  if (outlineRuntime) outlineRuntime.textContent = runtime;
  if (outlinePref) outlinePref.textContent = preferences || "—";
  if (outlinePrefRow) outlinePrefRow.hidden = !preferences;
  if (outlineSummary) outlineSummary.textContent = `${model} · ${skill} · 深度思考：${thinking === "开启" ? "已开启" : "未开启"}`;
  syncCaseContext(model);
}

function syncCaseContext(model) {
  const context = $("#case-context");
  if (!context) return;
  context.hidden = !ed.id;
  const query = ed.briefMeta?.user_query || ed.fullQuery || "";
  const modelBadge = $("#case-model-badge");
  modelBadge.hidden = !ed.id;
  modelBadge.title = `生成模型：${model}`;
  $("#case-model").textContent = model;
  $("#case-model").title = model;
  $("#case-query-preview").textContent = query || "—";
  $("#case-query-full").textContent = query || "—";
  const queryCopy = $("#case-query-copy");
  queryCopy.dataset.copyMessage = query;
  queryCopy.disabled = !query;
}

function renderStatus(p, phase) {
  delete $("#ed-status").dataset.dynamicChromeKey;
  const cls = p.status === "completed" ? "ph-done" : (["failed", "rejected", "interrupted", "paused_recoverable"].includes(p.status) ? "ph-failed" : "");
  const act = (!["completed", "failed", "rejected", "interrupted", "paused_recoverable"].includes(p.status) && actText(p)) || "";
  const badgeLabel = p.status === "completed" && p.training_admission === "rejected"
    ? "交付完成 · 训练未准入"
    : ["interrupted", "paused_recoverable"].includes(p.status)
    ? STATUS_LABEL[p.status]
    : (PHASE_LABEL[phase] || phase);
  const phaseBadge = ["delegating", "rendering"].includes(phase)
    ? ""
    : `<span class="badge ${cls}">${badgeLabel}</span>`;
  $("#ed-status").innerHTML =
    phaseBadge +
    (act ? `<span class="ed-act">${escapeHtml(act)}</span>` : "");
  syncTaskComposerState();
}

function currentElapsedSeconds() {
  if (ed.elapsedBase == null) return;
  const live = isStaticActiveStatus(ed.status);
  return Math.max(0, Math.round(ed.elapsedBase + (live ? (Date.now() - ed.elapsedAt) / 1000 : 0)));
}

function tick() {   // SSE 间隙本地走秒
  const s = currentElapsedSeconds();
  if (s == null) return;
  const el = $("#ed-elapsed");
  const statusText = `${STATUS_LABEL[ed.status] || ed.status}` +
    (ed.total ? ` · ${ed.rendered.size}/${ed.total} 页` : "");
  if (el && el.textContent !== statusText) el.textContent = statusText;
  if ($("#pr-elapsed").textContent !== fmtDur(s)) $("#pr-elapsed").textContent = fmtDur(s);
}

/* ----- 右侧生成过程栏 ----- */
function feedAdd(html, cls = "", timestamp = null) {
  const item = document.createElement("div");
  item.className = "pr-item " + cls;
  const raw = Number(timestamp);
  const t = Number.isFinite(raw) ? new Date(raw < 1e12 ? raw * 1000 : raw) : new Date();
  item.innerHTML = `<span class="pr-time">${pad2(t.getHours())}:${pad2(t.getMinutes())}</span>${html}`;
  $("#pr-feed").prepend(item);
}

function railDisclosureClosedHeight(details, summary) {
  const style = getComputedStyle(details);
  const border = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
  return summary.offsetHeight + border;
}

function setRailDisclosureOpen(details, open) {
  const summary = details.querySelector(":scope > summary");
  const body = details.querySelector(":scope > .pr-rail-body");
  if (!summary || !body || details.classList.contains("is-animating")) return;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reducedMotion || typeof details.animate !== "function") {
    details.open = open;
    return;
  }

  const startHeight = details.offsetHeight;
  if (open) details.open = true;
  const endHeight = open
    ? details.scrollHeight + (parseFloat(getComputedStyle(details).borderTopWidth) || 0)
      + (parseFloat(getComputedStyle(details).borderBottomWidth) || 0)
    : railDisclosureClosedHeight(details, summary);

  details.classList.add("is-animating");
  details.style.height = `${startHeight}px`;
  const shellAnimation = details.animate(
    [{ height: `${startHeight}px` }, { height: `${endHeight}px` }],
    { duration: open ? 380 : 340, easing: "cubic-bezier(.22,.8,.3,1)", fill: "both" },
  );
  const bodyAnimation = body.animate(
    open
      ? [{ opacity: 0, transform: "translateY(-5px) scaleY(.985)" }, { opacity: 1, transform: "none" }]
      : [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-4px) scaleY(.985)" }],
    { duration: open ? 320 : 250, easing: "cubic-bezier(.22,.8,.3,1)", fill: "both" },
  );
  shellAnimation.finished.catch(() => {}).then(() => {
    if (!open) details.open = false;
    details.classList.remove("is-animating");
    details.style.height = "";
    shellAnimation.cancel();
    bodyAnimation.cancel();
  });
}

function initRailDisclosureAnimations() {
  $$(".pr-rail-section").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    summary?.addEventListener("click", (event) => {
      event.preventDefault();
      setRailDisclosureOpen(details, !details.open);
    });
  });
}

function syncPageSpeechDrawer(n = Number(ed.sel) || 0) {
  const drawer = $("#pr-speech-section");
  const toggle = $("#page-speech-toggle");
  if (!drawer || !toggle) return;
  // Dynamic decks normally do not ship speech.md, and static Skills can omit
  // it too. Once any page proves that this deck has notes, however, keep the
  // bottom drawer mounted. Page changes then update only its contents instead
  // of making the whole control jump in and out of the preview layout.
  const hasAnySpeech = !!ed.hasAnySpeech
    || [...(ed.speechPages?.values?.() || [])].some(Boolean);
  const visible = !!ed.id && ed.workspaceView === "ppt" && n > 0
    && hasAnySpeech;
  drawer.hidden = !visible;
  // Default to a compact footer. An explicit "0" is written only after the
  // user opens it, so returning users still keep their own preference.
  const collapsed = localStorage.getItem("studio_speech_collapsed") !== "0";
  drawer.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
}

function setPageSpeechScope(n) {
  syncPageSpeechDrawer(n);
  if (n <= 0) {
    ed.speechScopePage = null;
    ed.speechRenderKey = "";
    return;
  }
  if (ed.speechScopePage === n) return;
  ed.speechScopePage = n;
  ed.speechRenderKey = "";
  const sourceToggle = $("#page-speech-source-toggle");
  if (sourceToggle) {
    sourceToggle.hidden = true;
    sourceToggle.setAttribute("aria-expanded", "false");
  }
  $("#pr-speech-state").textContent = "正在读取本页内容…";
  const content = $("#pr-speech-content");
  if (ed.hasAnySpeech && content.childElementCount) {
    content.classList.add("is-updating");
  } else {
    content.innerHTML = '<div class="pr-speech-empty">正在整理本页讲稿</div>';
  }
}

function setRailPageScope(n) {
  setPageSpeechScope(n);
  const history = $("#pr-page-history");
  const feed = $("#pr-feed");
  const rail = $("#proc-rail");
  const pageMode = ed.kind === "static" && Number(n) > 0;
  rail?.classList.toggle("page-history-mode", pageMode);
  if (history) history.hidden = !pageMode;
  if (feed) feed.hidden = pageMode;
  if (!pageMode) {
    ed.historyScopePage = null;
    ed.historyRenderKey = "";
    return;
  }
  // select()/livefeed polling may revisit the same page many times. Do not
  // replace the rail with loading placeholders unless the selected page
  // actually changed; replacing it collapses details and restarts animations.
  if (ed.historyScopePage === n) return;
  ed.historyScopePage = n;
  ed.historyRenderKey = "";
  $("#pr-history-title").textContent = `第 ${pad2(n)} 页 · 页面详情`;
  const cached = ed.pageHistoryCache?.get(Number(n));
  if (cached?.data) {
    renderPageHistory(cached.data, Number(n));
    return;
  }
  $("#pr-history-count").textContent = "正在读取真实渲染轨迹…";
  $("#pr-history-list").innerHTML = '<div class="pr-history-empty"><i></i><span>正在整理本页的渲染与视觉检查记录</span></div>';
}

function historyVerdict(item) {
  const judgment = String(item.judgment || "").trim();
  if (judgment.length >= 18) return judgment;
  return String(item.prompt || judgment || "该版本已完成视觉检查。请展开查看检查维度。").trim();
}

function speechMarkdownBlocks(value) {
  const blocks = String(value || "").trim().split(/\n\s*\n/).filter(Boolean);
  return blocks.map((block) => {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length && lines.every((line) => /^[-*+]\s+/.test(line))) {
      return `<ul>${lines.map((line) => `<li>${escapeHtml(line.replace(/^[-*+]\s+/, ""))}</li>`).join("")}</ul>`;
    }
    return `<p>${lines.map((line) => escapeHtml(line)).join("<br>")}</p>`;
  }).join("");
}

function cleanSpeechNotes(value, title = "") {
  const expectedTitle = String(title || "").replace(/^#+\s*/, "").trim();
  const lines = String(value || "").split("\n");
  while (lines.length) {
    const first = lines[0].trim();
    const plain = first.replace(/^#{1,6}\s*/, "").trim();
    if (!first) { lines.shift(); continue; }
    if (/^(?:slide|page)\s*0*\d+(?:\s*[：:·|｜—–-].*)?$/i.test(plain)) {
      lines.shift();
      continue;
    }
    if (expectedTitle && plain === expectedTitle) {
      lines.shift();
      continue;
    }
    break;
  }
  return lines.join("\n").trim();
}

function renderPageSpeech(speech, n) {
  const state = $("#pr-speech-state");
  const content = $("#pr-speech-content");
  const sourceToggle = $("#page-speech-source-toggle");
  const hasSpeech = !!speech?.exists && [speech.notes, speech.evidence]
    .some((value) => String(value || "").trim());
  const hasEvidence = !!String(speech?.evidence || "").trim();
  if (sourceToggle) {
    sourceToggle.hidden = !hasEvidence;
    if (!hasEvidence) sourceToggle.setAttribute("aria-expanded", "false");
  }
  ed.speechPages ||= new Map();
  ed.speechPages.set(Number(n), hasSpeech);
  if (hasSpeech) ed.hasAnySpeech = true;
  syncPageSpeechDrawer(n);
  const renderKey = JSON.stringify({ n, status: ed.status, speech: speech || null });
  if (ed.speechRenderKey === renderKey) return;
  ed.speechRenderKey = renderKey;
  content.classList.remove("is-updating");
  if (!hasSpeech) {
    const active = ed.kind === "dynamic" ? ed.status === "running" : isStaticActiveStatus(ed.status);
    state.textContent = active ? "等待本页讲稿" : "本页暂无讲稿";
    content.innerHTML = `<div class="pr-speech-empty">${isStaticActiveStatus(ed.status)
      || (ed.kind === "dynamic" && ed.status === "running")
      ? "讲稿生成后会自动显示在这里"
      : "这页暂时没有讲稿"}</div>`;
    return;
  }
  state.textContent = "讲述内容与来源";
  const notes = speechMarkdownBlocks(cleanSpeechNotes(speech.notes, speech.title));
  const evidence = speechMarkdownBlocks(speech.evidence);
  const sourceOpen = hasEvidence && sourceToggle?.getAttribute("aria-expanded") === "true";
  content.innerHTML = `
    <div class="pr-speech-block">${notes || "<p>暂无讲述内容</p>"}</div>
    ${hasEvidence ? `<div class="pr-speech-evidence-body" id="page-speech-evidence"${sourceOpen ? "" : " hidden"}>${evidence || "<p>暂无来源信息</p>"}</div>` : ""}`;
}

function normalizeVisionTechnicalDetail(value) {
  let text = String(value || "").trim()
    .replace(/^---+\s*/, "")
    .replace(/``(?:text)?\s*/gi, "\n")
    .replace(/\s*``\s*$/g, "")
    .replace(/([。；])\s*(?=(?:身份(?:声明)?|产出)\s*[:：])/g, "$1\n")
    .replace(/\s+(?=(?:身份(?:声明)?|产出)\s*[:：])/g, "\n")
    .replace(/\s+(?=(?:group|status|pages|renders|hard_issues|summary)\s*:)/gi, "\n");
  text = text.split("\n").map((line) => {
    const trimmed = line.trim();
    const field = trimmed.match(/^(身份(?:声明)?|产出|group|status|pages|renders|hard_issues|summary)\s*[:：]\s*(.*)$/i);
    if (!field) return trimmed;
    const labels = {
      "身份": "执行角色", "身份声明": "执行角色", "产出": "交付文件",
      group: "页面组", status: "状态", pages: "页码", renders: "渲染文件",
      hard_issues: "严重问题", summary: "完整说明",
    };
    const key = labels[field[1]] || labels[field[1].toLowerCase()] || field[1];
    return `- **${key}：** ${field[2]}`;
  }).filter(Boolean).join("\n");
  return text;
}

function visionJudgmentPresentation(value) {
  const text = cleanAgentText(value);
  if (!text) return { summary: "", detail: "" };
  const markers = [/\s+---+\s+/, /\s+身份(?:声明)?\s*[:：]/i, /\s+``(?:text)?\s*/i, /\s+```(?:text)?\s*/i];
  let splitAt = -1;
  markers.forEach((pattern) => {
    const match = pattern.exec(text);
    if (match && (splitAt < 0 || match.index < splitAt)) splitAt = match.index;
  });
  if (splitAt < 0) return { summary: text, detail: "" };
  return {
    summary: text.slice(0, splitAt).trim(),
    detail: normalizeVisionTechnicalDetail(text.slice(splitAt).trim()),
  };
}

function renderPageHistory(data, n) {
  if (ed.kind !== "static" || ed.sel !== n) return;
  const items = data.items || [];
  renderPageSpeech(data.speech || {}, n);
  const countText = items.length
    ? `${data.total || items.length} 次真实视觉检查`
    : (isStaticActiveStatus(ed.status) ? "等待首次视觉检查" : "暂无视觉检查记录");
  if ($("#pr-history-count").textContent !== countText) $("#pr-history-count").textContent = countText;
  const list = $("#pr-history-list");
  const renderKey = JSON.stringify({ n, status: ed.status, total: data.total || 0, items });
  if (ed.historyRenderKey === renderKey) return;
  const scroll = list.closest(".pr-history-scroll");
  const previousTop = scroll?.scrollTop || 0;
  const promptOpen = $$(".pr-history-prompt", list).map((details) => details.open);
  const judgmentOpen = $$(".pr-history-judgment-details", list).map((details) => details.open);
  ed.historyRenderKey = renderKey;
  if (!items.length) {
    list.innerHTML = `<div class="pr-history-empty"><i></i><span>${isStaticActiveStatus(ed.status)
      ? "页面生成后，渲染图与 Vision 判断会实时出现在这里"
      : "这页没有保存独立的 Vision 检查快照"}</span></div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const review = item.stage === "review";
    const revisionNo = Number(item.revision_no || 0);
    const roundLabel = revisionNo > 0 ? `第 ${revisionNo} 轮修改` : "初版";
    const hasJudgment = String(item.judgment || "").trim().length >= 18;
    const verdict = hasJudgment
      ? historyVerdict(item)
      : "本次 Vision 检查未返回可见判断，仅保存了检查问题与当时的页面快照。";
    const judgment = visionJudgmentPresentation(verdict);
    const prompt = String(item.prompt || "").trim();
    const changed = Number(item.changes || 0);
    return `<article class="pr-history-card${review ? " review" : ""}">
      <div class="pr-history-meta">
        <span class="pr-history-stage">${roundLabel} · ${review ? (() => { const rs = (agentTimingFor("review") || {}).status; return rs === "partial" ? "整册 Review · 有界降级" : rs === "failed" ? "整册 Review · 未完成" : rs === "running" ? "整册 Review · 进行中" : "整册 Review"; })() : `页面自检 · V${item.version || 1}`}</span>
        <time>${escapeHtml(item.time || "")}</time>
      </div>
      ${item.image_url ? `<button type="button" class="pr-history-shot" aria-label="查看第 ${item.version || 1} 版渲染大图"><img src="${escapeHtml(item.image_url)}" alt="第 ${n} 页第 ${item.version || 1} 版渲染"></button>` : ""}
      <div class="pr-history-verdict${hasJudgment ? "" : " is-missing"}"><span>Vision 检查结果</span><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(judgment.summary || verdict)}</div></div>
      ${judgment.detail ? `<details class="pr-history-judgment-details"><summary>查看完整判断<i aria-hidden="true"></i></summary><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(judgment.detail)}</div></details>` : ""}
      ${changed ? `<div class="pr-history-change"><i>↻</i><span><b>修改记录 · ${changed} 次</b>${(item.change_notes || []).length
        ? `<ul>${item.change_notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`
        : `<em>根据视觉检查结果完成页面调整</em>`}${item.version < items.length ? `<em>完成后重新渲染并进入下一轮检查</em>` : ""}</span></div>` : ""}
      ${prompt ? `<details class="pr-history-prompt"><summary>查看检查项</summary><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(prompt)}</div></details>` : ""}
    </article>`;
  }).join("");
  $$(".pr-history-prompt", list).forEach((details, index) => {
    if (promptOpen[index]) details.open = true;
  });
  $$(".pr-history-judgment-details", list).forEach((details, index) => {
    if (judgmentOpen[index]) details.open = true;
  });
  if (scroll) requestAnimationFrame(() => { scroll.scrollTop = previousTop; });
}

async function loadPageHistory(n, { quiet = false, prefetch = false, force = false } = {}) {
  if (ed.kind !== "static" || !ed.id || n <= 0) return;
  ed.pageHistoryCache ||= new Map();
  ed.pageHistoryPending ||= new Map();
  const page = Number(n);
  const now = Date.now();
  const active = isStaticActiveStatus(ed.status);
  const cached = ed.pageHistoryCache.get(page);
  const cacheTtl = active ? 5000 : 30000;
  if (!prefetch && cached?.data && ed.sel === page) renderPageHistory(cached.data, page);
  if (!force && cached?.data && cached.status === ed.status && now - cached.fetchedAt < cacheTtl) {
    return cached.data;
  }
  const request = prefetch ? ed.historyRequest : ++ed.historyRequest;
  let pending = ed.pageHistoryPending.get(page);
  if (!pending) {
    pending = jget(`${trajectoryMode ? trajectoryDeckApi(ed.id, "/page-history") : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/page-history`}?n=${page}`);
    ed.pageHistoryPending.set(page, pending);
  }
  try {
    const data = await pending;
    ed.pageHistoryCache.set(page, { data, status: ed.status, fetchedAt: Date.now() });
    if (!prefetch && (request !== ed.historyRequest || ed.kind !== "static" || ed.sel !== page)) return data;
    if (!prefetch) renderPageHistory(data, page);

    // Completed decks are immutable most of the time. Warm the neighboring
    // pages only after the visible page is ready, so the next click can render
    // immediately without competing with live generation requests.
    if (!prefetch && !active) {
      const warm = () => [page - 1, page + 1]
        .filter((candidate) => candidate > 0 && candidate <= Number(ed.total || 0))
        .forEach((candidate) => {
          if (!ed.pageHistoryCache.has(candidate)) loadPageHistory(candidate, { quiet: true, prefetch: true });
        });
      if (typeof requestIdleCallback === "function") requestIdleCallback(warm, { timeout: 1200 });
      else setTimeout(warm, 120);
    }
    return data;
  } catch (error) {
    if (!prefetch && (request !== ed.historyRequest || ed.kind !== "static" || ed.sel !== page || quiet)) return;
    if (prefetch || quiet) return;
    $("#pr-history-count").textContent = "记录读取失败";
    $("#pr-history-list").innerHTML = `<div class="pr-history-empty"><span>${escapeHtml(error.message)}</span></div>`;
    $("#pr-speech-state").textContent = "讲稿读取失败";
    $("#pr-speech-content").innerHTML = `<div class="pr-speech-empty">${escapeHtml(error.message)}</div>`;
  } finally {
    if (ed.pageHistoryPending.get(page) === pending) ed.pageHistoryPending.delete(page);
  }
}

function speechFromMarkdown(text, n, source = "speech.md") {
  const value = String(text || "");
  const headingPattern = /^#{1,3}\s+(?:(?:slide|page)\s*0*(\d+)|第\s*0*(\d+)\s*页)(?:\s*[—–:：|｜·-]\s*([^\n]+))?\s*$/gim;
  const headings = [...value.matchAll(headingPattern)];
  const index = headings.findIndex((match) => Number(match[1] || match[2]) === Number(n));
  if (index < 0) return { exists: false, page: n, source };
  const heading = headings[index];
  const start = Number(heading.index) + heading[0].length;
  const end = index + 1 < headings.length ? Number(headings[index + 1].index) : value.length;
  let body = value.slice(start, end).trim();
  const evidencePattern = /^#{2,4}\s+(?:evidence\s+and\s+sources|证据(?:与来源)?|来源(?:与证据)?|参考资料(?:[（(]不朗读[）)])?)\s*$/im;
  const evidenceMatch = evidencePattern.exec(body);
  const notes = (evidenceMatch ? body.slice(0, evidenceMatch.index) : body)
    .replace(/^#{2,4}\s+(?:讲述内容|演讲稿|speaker\s+notes?)\s*$/im, "").trim();
  const evidence = evidenceMatch ? body.slice(evidenceMatch.index + evidenceMatch[0].length).trim() : "";
  return { exists: !!(notes || evidence), page: n, title: String(heading[3] || "").trim(), notes, evidence, source };
}

async function loadDynamicSpeechFile(n) {
  for (const rel of ["speech.md", "plan/speech.md"]) {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/dynamic/files/${encodeURIComponent(ed.id)}/${rel}`, { credentials: "same-origin" });
    if (response.ok) return speechFromMarkdown(await response.text(), n, rel);
    if (response.status !== 404) throw new Error(`讲稿读取失败 (${response.status})`);
  }
  return { exists: false, page: n };
}

async function loadDynamicPageSpeech(n, { quiet = false } = {}) {
  if (ed.kind !== "dynamic" || !ed.id || n <= 0) return;
  const request = ++ed.speechRequest;
  try {
    const data = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/page-speech?conv_id=${encodeURIComponent(ed.id)}&n=${n}`);
    if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
    renderPageSpeech(data || {}, n);
  } catch (error) {
    if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
    try {
      const speech = await loadDynamicSpeechFile(n);
      if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
      renderPageSpeech(speech, n);
      return;
    } catch {}
    ed.speechPages ||= new Map();
    if (!ed.speechPages.has(Number(n))) ed.speechPages.set(Number(n), false);
    syncPageSpeechDrawer(n);
    if (quiet) return;
    $("#pr-speech-state").textContent = "本页暂无讲稿";
    $("#pr-speech-content").innerHTML = '<div class="pr-speech-empty">这页暂时没有讲稿</div>';
  }
}

function railApply(p, phase) {
  const terminal = ["completed", "failed", "rejected", "interrupted", "paused_recoverable", "not_started"].includes(p.status);

  $("#pr-elapsed").textContent = p.elapsed_s != null ? fmtDur(p.elapsed_s) : "";
  $("#pr-count").innerHTML = ed.total
    ? `${ed.rendered.size}<small>/${ed.total}</small>` : (p.status === "completed" ? "✓" : "–");
  const pct = ed.total ? Math.round((ed.rendered.size / ed.total) * 100)
    : (p.status === "completed" ? 100 : 4);
  $("#pr-bar").style.width = (p.status === "completed" ? 100 : pct) + "%";
  syncMuralRuntimeCard(p);

  // 阶段时间线
  const cur = p.status === "completed" ? "done" : PHASE_STEP[phase];
  const idx = STEP_ORDER.indexOf(cur);
  $$("#pr-steps li").forEach((li) => {
    const i = STEP_ORDER.indexOf(li.dataset.k);
    li.className = i < idx || p.status === "completed" ? "done" : (i === idx ? "now" : "");
  });

  // 实时动作
  const act = !terminal ? actText(p) : "";
  $("#pr-act").textContent = act || (terminal ? "" : (
    p.status === "waiting" ? "等待上一版完成…"
      : (p.status === "queued" ? "排队中…" : "正在启动引擎…")
  ));

  // 事件流:阶段切换(按显示名去重,delegating/rendering 同属「并行生成」只记一次)+ 每页渲染完成
  const lbl = PHASE_LABEL[phase];
  if (!terminal && lbl && lbl !== ed.lastPhase) {
    if (ed.lastPhase !== null || phase !== "starting")
      feedAdd(`<span class="pr-txt">进入「${lbl}」</span>`);
    ed.lastPhase = lbl;
  }
  (p.rendered || []).forEach((n) => {
    if (ed.feedSeen.has(n)) return;
    ed.feedSeen.add(n);
    const src = fileUrl(n);
    feedAdd(`<img class="pr-thumb" src="${src}" alt=""><span class="pr-txt">第 ${n} 页渲染完成</span>`, "slide");
  });
  if (terminal && !ed.finalized) {
    ed.finalized = true;
    if (p.status === "completed")
      feedAdd(`<span class="pr-txt pr-ok">✓ 全部完成 · 共 ${ed.rendered.size} 页${p.elapsed_s != null ? " · 用时 " + fmtDur(p.elapsed_s) : ""}</span>`);
  }
}

function syncMuralRuntimeCard(p) {
  const card = $("#pr-runtime");
  const mural = p?.mural_runtime || {};
  if (!card || !["mural.runtime-summary.v1", "mural.webui-runtime-projection.v1"].includes(mural.schema)) return;
  const runtime = mural;
  const states = mural.state_counts || {};
  const stateText = [
    states.active ? `${states.active} 运行` : "",
    states.waiting ? `${states.waiting} 等待` : "",
    states.blocked ? `${states.blocked} 阻塞` : "",
    states.terminal ? `${states.terminal} 已闭合` : "",
  ].filter(Boolean).join(" · ");
  const concurrency = Number(runtime.effective_concurrency);
  const detail = [
    stateText,
    Number.isFinite(concurrency) && concurrency > 0
      ? `模型有效并发 ×${concurrency.toFixed(2)}` : "",
    runtime.model_concurrency_observability === "unavailable"
      ? `Child 峰值并发 ${runtime.max_child_concurrency ?? "—"}（模型并发未观测）` : "",
    runtime.provider_queue_observability === "unavailable"
      ? "服务端排队未提供" : "",
  ].filter(Boolean).join(" · ");
  card.dataset.kind = "static";
  card.querySelector("b").textContent = "MURAL 任务状态";
  card.querySelector("i").textContent = detail || "等待首个 Owner 任务";
  card.title = runtime.observed_critical_path_s != null
    ? `已观测关键路径 ${fmtDur(runtime.observed_critical_path_s)}`
    : "";
}

function renderActions(status) {
  if (ed.kind === "dynamic") {
    const running = status === "running";
    const ready = status === "completed" && !!ed.dynamicDeckUrl;
    const deckUrl = ed.dynamicDeckUrl || "";
    const actions = $("#ed-actions");
    delete actions.dataset.staticRenderKey;
    const renderKey = JSON.stringify([ed.id, status, ready, deckUrl]);
    if (actions.dataset.dynamicRenderKey === renderKey) return;
    actions.dataset.dynamicRenderKey = renderKey;
    actions.innerHTML =
      (running ? `<button class="btn-cancel" id="cancel-btn">停止生成</button>` : "") +
      (ready
        ? `<button class="btn-ghost" id="overview-btn" type="button">预览</button>
           <button class="btn-ghost" id="fullscreen-btn" type="button">播放</button>
           <div class="export-menu-wrap">
             <button class="btn-dl export-trigger" id="export-trigger" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="export-menu">
               <span>导出</span><span class="export-chevron" aria-hidden="true"></span>
             </button>
             <div class="export-menu" id="export-menu" role="menu" hidden>
               <button class="export-menu-item utility-action" id="share-btn" type="button" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">↗</span><span class="export-menu-copy"><b>分享</b><small>复制或调用系统分享</small></span>
               </button>
               <button class="export-menu-item utility-action" id="copy-output-btn" type="button" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">⌘</span><span class="export-menu-copy"><b>复制输出路径</b><small>复制服务器上的产物目录</small></span>
               </button>
               <span class="export-menu-separator" aria-hidden="true"></span>
               <a class="export-menu-item" href="${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/download?conv_id=${encodeURIComponent(ed.id)}" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">↓</span><span class="export-menu-copy"><b>下载 .zip</b><small>包含动态 HTML、图片与本地资源</small></span>
               </a>
             </div>
           </div>`
        : "");
    bindUtilityActions();
    bindExportMenu();
    bindPresentationFullscreen();
    const ob = $("#overview-btn");
    if (ob) ob.onclick = openOverviewModal;
    const cb = $("#cancel-btn");
    if (cb) cb.onclick = async () => {
      cb.disabled = true; cb.textContent = "停止中…";
      ed.stopRequested = true;
      ed.status = "stopped";
      renderDynamicChrome();
      renderActions("stopped");
      showStudioNotice("生成已中断", { type: "info", duration: 2800 });
      try {
        await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/stop`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conv_id: ed.id }),
        });
      } catch (error) {
        showStudioNotice(`停止请求发送失败：${error.message}`, { type: "error" });
      }
    };
    return;
  }
  const actions = $("#ed-actions");
  delete actions.dataset.dynamicRenderKey;
  const terminal = ["completed", "failed", "rejected", "interrupted", "paused_recoverable", "not_started"].includes(status);
  const recoverable = status === "paused_recoverable"
    || (ed.resumeEligible && ["failed", "interrupted"].includes(status));
  const failed = status === "failed" || status === "rejected" || status === "interrupted" || recoverable;
  const isV1 = ed.briefMeta?.skill_version === "mural-presenter-v1"
    && ed.briefMeta?.verified_delivery_download === true;
  const exporting = pendingDeckExports.has(String(ed.id));
  const renderKey = JSON.stringify([ed.id, status, ed.resumeEligible, ed.pptOutput, ed.staticDeckUrl, isV1, exporting]);
  if (actions.dataset.staticRenderKey === renderKey) return;
  actions.dataset.staticRenderKey = renderKey;
  const dynamicHtml = ed.pptOutput === "dynamic_html";
  actions.innerHTML =
    (!terminal ? `<button class="btn-cancel" id="cancel-btn">取消</button>` : "") +
    (recoverable
      ? `<button class="btn-retry" id="resume-btn">↻ 从检查点继续</button>`
      : (failed ? `<button class="btn-retry" id="retry-btn">↻ 重新生成</button>` : "")) +
    (recoverable
      ? `<button class="btn-ghost" id="retry-btn">从头重新生成</button>`
      : "") +
    (status === "completed"
      ? `<button class="btn-ghost" id="overview-btn">预览</button>
         <button class="btn-ghost" id="fullscreen-btn" type="button">播放</button>
         <div class="export-menu-wrap">
           <button class="btn-dl export-trigger" id="export-trigger" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="export-menu">
             <span>${exporting ? "导出中…" : "导出"}</span><span class="export-chevron" aria-hidden="true"></span>
           </button>
           <div class="export-menu" id="export-menu" role="menu" hidden>
             <button class="export-menu-item utility-action" id="share-btn" type="button" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">↗</span><span class="export-menu-copy"><b>分享</b><small>复制或调用系统分享</small></span>
             </button>
             <button class="export-menu-item utility-action" id="copy-output-btn" type="button" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">⌘</span><span class="export-menu-copy"><b>复制输出路径</b><small>复制服务器上的产物目录</small></span>
             </button>
             <span class="export-menu-separator" aria-hidden="true"></span>
             ${isV1 ? [
               ["html", "HTML", "单文件离线演示"], ["pdf", "PDF", "便于阅读与打印"],
               ["pptx", "高保真 PPTX", "每页一张完整图片"],
               ["pptx-editable", "可编辑 PPTX", "原生文字与基础元素；复杂效果保留为局部图片"],
               ["zip", "便携 ZIP", "演示与必要资源"],
             ].map(([format, label, note]) => `<button class="export-menu-item" data-export-format="${format}" type="button" role="menuitem" ${exporting ? "disabled" : ""}>
               <span class="export-menu-icon export-file-icon" aria-hidden="true">↓</span><span class="export-menu-copy"><b>导出 ${label}</b><small>${note} · 按需生成</small></span>
             </button>`).join("") : (dynamicHtml
               ? ""
               : `<a class="export-menu-item" href="${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/pptx" role="menuitem">
                    <span class="export-menu-icon export-file-icon" aria-hidden="true">P</span><span class="export-menu-copy"><b>导出 .pptx</b><small>下载 PowerPoint 文件</small></span>
                  </a>`) + `<a class="export-menu-item" href="${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/download" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">↓</span><span class="export-menu-copy"><b>下载 .zip</b><small>下载完整工程与资源</small></span>
             </a>`}
             ${isV1 ? `<a class="export-menu-item" href="${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/project-download" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">⌘</span><span class="export-menu-copy"><b>下载作者工程包</b><small>页面源码、规划与研究材料；非便携演示包</small></span>
             </a>` : ""}
             <span class="export-menu-separator" aria-hidden="true"></span>
             <button class="export-menu-item utility-action" id="font-resolution-btn" type="button" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">Aa</span><span class="export-menu-copy"><b>字体解析</b><small>查看每角色请求 / 实际 / 状态</small></span>
             </button>
           </div>
         </div>` : "");
  bindUtilityActions();
  bindExportMenu();
  bindFontResolution();
  bindPresentationFullscreen();
  const ob = $("#overview-btn");
  if (ob) ob.onclick = openOverviewModal;
  const cb = $("#cancel-btn");
  if (cb) cb.onclick = async () => {
    cb.disabled = true;
    ed.stopRequested = true;
    ed.status = "interrupted";
    renderStatus({ status: "interrupted" }, "failed");
    renderActions("interrupted");
    showStudioNotice("生成已中断", { type: "info", duration: 2800 });
    try {
      await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/cancel`, { method: "POST" });
    } catch (error) {
      showStudioNotice(`停止请求发送失败：${error.message}`, { type: "error" });
    }
  };
  const rb = $("#retry-btn");
  if (rb) rb.onclick = async () => {
    const retryLabel = recoverable ? "从头重新生成" : "↻ 重新生成";
    rb.disabled = true; rb.textContent = "排队中…";
    try {
      const r = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/retry`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || "失败");
      await loadDecks(ed.id);
      openDeck(ed.id);                       // 重新进入直播
    } catch (e) { alert("重新生成失败: " + e.message); rb.disabled = false; rb.textContent = retryLabel; }
  };
  const resume = $("#resume-btn");
  if (resume) resume.onclick = async () => {
    resume.disabled = true; resume.textContent = "恢复中…";
    try {
      const r = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/resume`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || "失败");
      await loadDecks(ed.id);
      openDeck(ed.id);
    } catch (e) {
      alert("断点续跑失败: " + e.message);
      resume.disabled = false;
      resume.textContent = "↻ 从检查点继续";
    }
  };
}

const pendingDeckExports = new Set();

async function downloadDeckExport(format) {
  const deckId = String(ed.id);
  if (pendingDeckExports.has(deckId) || !["html", "pdf", "pptx", "pptx-editable", "zip"].includes(format)) return;
  pendingDeckExports.add(deckId);
  renderActions(ed.status);
  const notice = showStudioNotice(`正在生成 ${format.toUpperCase()}，完成后将自动下载…`, { type: "info", duration: 0 });
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(deckId)}/export/${format}`, { method: "POST" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = format === "pptx-editable" ? `presentation_${deckId}_editable.pptx` : `presentation_${deckId}.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (format === "pptx-editable") {
      const report = response.headers.get("X-Mural-Editability-Report");
      const textCount = response.headers.get("X-Mural-Editable-Text");
      const fallbackCount = response.headers.get("X-Mural-Raster-Fallbacks");
      const result = showStudioNotice(`可编辑 PPTX 已生成：${textCount} 个原生文本对象，${fallbackCount} 处复杂效果保留为局部图片。字体未嵌入，接收端需安装报告所列字体。`, { type: "info", duration: 0 });
      const reportLink = document.createElement("a");
      reportLink.href = `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(deckId)}/file?rel=${report}`;
      reportLink.textContent = " 查看编辑覆盖报告";
      reportLink.target = "_blank";
      reportLink.rel = "noopener";
      result.querySelector(".studio-notice-copy").append(reportLink);
    } else {
      showStudioNotice(`${format.toUpperCase()} 已生成，开始下载`, { type: "info" });
    }
  } catch (error) {
    showStudioNotice(`导出失败：${error.message}`, { type: "error" });
  } finally {
    pendingDeckExports.delete(deckId);
    dismissStudioNotice(notice);
    if (String(ed.id) === deckId) renderActions(ed.status);
  }
}

function closeExportMenu() {
  const trigger = $("#export-trigger");
  const menu = $("#export-menu");
  if (!trigger || !menu) return;
  menu.hidden = true;
  trigger.classList.remove("open");
  trigger.setAttribute("aria-expanded", "false");
}

function bindExportMenu() {
  const trigger = $("#export-trigger");
  const menu = $("#export-menu");
  if (!trigger || !menu) return;
  trigger.onclick = (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", String(open));
  };
  menu.onclick = (event) => {
    event.stopPropagation();
    const button = event.target.closest("[data-export-format]");
    if (button && !button.disabled) downloadDeckExport(button.dataset.exportFormat);
    if (event.target.closest(".export-menu-item")) closeExportMenu();
  };
}

// Per-role font resolution viewer. Reads the deck's manifest v5 resolutions via
// the backend and renders requested / resolved / status(label) / license with
// TEXT labels (not colour-only). Self-contained: no dependency on the trace
// panel (which task C owns).
const FONT_KIND_TONE = {
  exact_builtin: "ok", exact_alias: "ok", exact_user_upload: "ok",
  audited_substitute: "ok", route_default: "note", portable_fallback: "warn",
  system_fallback: "error", unresolved: "error",
};
function bindFontResolution() {
  const button = $("#font-resolution-btn");
  if (!button) return;
  button.onclick = async () => {
    try {
      const data = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/font-resolution`);
      if (!data.ready || !(data.resolutions || []).length) {
        showStudioNotice("字体解析尚未生成（需要至少一次打包渲染）", { type: "info", duration: 3600 });
        return;
      }
      showFontResolutionModal(data);
    } catch (error) {
      showStudioNotice(`读取字体解析失败：${error.message}`, { type: "error" });
    }
  };
}
function showFontResolutionModal(data) {
  document.getElementById("font-resolution-modal")?.remove();
  const rows = (data.resolutions || []).map((r) => {
    const tone = FONT_KIND_TONE[r.kind] || "note";
    const license = r.license === "user-provided" ? "用户授权"
      : r.license === "OFL-1.1" ? "OFL-1.1" : (r.license || "—");
    const mapping = r.mapping_id ? ` · ${escapeHtml(r.mapping_id)}` : "";
    return `<tr>
      <td>${escapeHtml(r.role || "")}</td>
      <td>${escapeHtml(r.requested || "—")}</td>
      <td>${escapeHtml(r.resolved || "—")}</td>
      <td><span class="font-res-tag font-res-${tone}">${escapeHtml(r.label || r.kind || "")}</span>${mapping}</td>
      <td>${escapeHtml(license)}</td>
    </tr>`;
  }).join("");
  const cov = data.coverage || {};
  const summary = `请求 ${cov.request_count ?? "—"} · 精确 ${cov.exact_count ?? 0} · 用户字体 ${cov.user_upload_count ?? 0} · 已审核替代 ${cov.substitute_count ?? 0} · fallback ${cov.fallback_count ?? 0} · 未解析 ${cov.unresolved_count ?? 0}`;
  const modal = document.createElement("div");
  modal.id = "font-resolution-modal";
  modal.className = "studio-modal-backdrop";
  modal.innerHTML = `<div class="studio-modal font-res-modal" role="dialog" aria-modal="true" aria-label="字体解析">
    <div class="font-res-head"><strong>字体解析</strong><button type="button" class="font-res-close" aria-label="关闭">×</button></div>
    <small class="font-res-summary">${escapeHtml(summary)}</small>
    <table class="font-res-table"><thead><tr><th>角色</th><th>请求</th><th>实际</th><th>状态</th><th>许可</th></tr></thead><tbody>${rows}</tbody></table>
    <small class="font-res-footnote">未审核 fallback 表示商用/未登记字体名已替换为可再分发字体（目录条目均为 proposed，未做视觉签核）。</small>
  </div>`;
  const close = () => modal.remove();
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  modal.querySelector(".font-res-close").onclick = close;
  document.body.appendChild(modal);
}

function bindPresentationFullscreen() {
  const button = $("#fullscreen-btn");
  if (!button) return;
  button.onclick = () => {
    const stage = $("#canvas");
    const deckUrl = ed.kind === "dynamic" ? ed.dynamicDeckUrl : ed.staticDeckUrl;
    if (!stage || !deckUrl) return;

    // Fullscreen must be requested synchronously from the click gesture. Move
    // from the 00 progress surface to a real slide first, then promote the
    // existing preview stage to the monitor's fullscreen top layer.
    ed.workspaceView = "ppt";
    ed.viewMode = "ppt";
    localStorage.setItem("studio_page_view", ed.viewMode);
    select(Math.max(1, Number(ed.sel) || 1), { byUser: true });

    const request = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (!request) {
      alert("当前浏览器不支持显示器全屏，请使用最新版 Chrome 或 Edge。");
      return;
    }
    try {
      const pending = request.call(stage);
      if (pending?.then) {
        pending.then(() => {
          scheduleDeckViewportFit();
          setTimeout(() => scheduleDeckViewportFit(), 180);
        }).catch(() => alert("浏览器阻止了全屏，请允许该页面进入全屏后重试。"));
      }
    } catch {
      alert("浏览器阻止了全屏，请允许该页面进入全屏后重试。");
    }
  };
}

function legacyCopyText(text) {
  const active = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none";
  document.body.appendChild(textarea);
  textarea.focus({ preventScroll: true });
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  let copied = false;
  try { copied = document.execCommand("copy"); }
  finally {
    textarea.remove();
    active?.focus?.({ preventScroll: true });
  }
  if (!copied) throw new Error("legacy clipboard copy was rejected");
}

async function writeClipboardText(text) {
  if (window.isSecureContext && navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
  else legacyCopyText(text);
}

function currentShareUrl() {
  const url = new URL(location.href);
  url.search = "";
  url.hash = `${ed.kind}-${encodeURIComponent(ed.id)}`;
  return url.toString();
}

function setTemporaryButtonText(button, text, fallback, delay = 1500) {
  if (!button) return;
  if (!button._fallbackHtml) button._fallbackHtml = button.innerHTML;
  button.textContent = text;
  clearTimeout(button._labelTimer);
  button._labelTimer = setTimeout(() => {
    if (!button.isConnected) return;
    if (button._fallbackHtml) button.innerHTML = button._fallbackHtml;
    else button.textContent = fallback;
  }, delay);
}

async function shareCurrentDeck() {
  const button = $("#share-btn");
  const url = currentShareUrl();
  if (navigator.share) {
    try {
      await navigator.share({ title: $("#ed-title")?.textContent || "MURAL Presenter", text: "查看这份 MURAL Presenter 演示", url });
      setTemporaryButtonText(button, "已分享", "↗ 分享");
      return;
    } catch (error) {
      if (error?.name === "AbortError") return;
    }
  }
  try {
    await writeClipboardText(url);
    setTemporaryButtonText(button, "链接已复制", "↗ 分享");
  } catch (error) {
    console.warn("Share link copy failed", error);
    setTemporaryButtonText(button, "复制失败", "↗ 分享", 1900);
  }
}

async function copyOutputLocation() {
  const button = $("#copy-output-btn");
  if (!button || !ed.id) return;
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    const endpoint = ed.kind === "dynamic"
      ? `${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/output-location?conv_id=${encodeURIComponent(ed.id)}`
      : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}/output-location`;
    const payload = await jget(endpoint);
    await writeClipboardText(payload.path || "");
    setTemporaryButtonText(button, "目录已复制", "⌘ 复制输出目录");
  } catch (error) {
    console.warn("Output location copy failed", error);
    setTemporaryButtonText(button, "复制失败", "⌘ 复制输出目录", 1900);
  } finally {
    button.disabled = false;
  }
}

function bindUtilityActions() {
  const share = $("#share-btn");
  const output = $("#copy-output-btn");
  if (share) share.onclick = shareCurrentDeck;
  if (output) output.onclick = copyOutputLocation;
}

async function copyOutlineQuery(button) {
  const text = button?.dataset.copyMessage || $("#outline-user-query")?.textContent?.trim() || outlineBriefData().query;
  if (!text) return;
  try {
    await writeClipboardText(text);
    setTemporaryButtonText(button, "已复制", "复制", 1200);
  } catch (error) {
    console.warn("Query copy failed", error);
    setTemporaryButtonText(button, "复制失败", "复制", 1800);
  }
}

function renderOverviewGrid() {
  const total = ed.total || ed.rendered.size || 0;
  const grid = $("#overview-grid");
  $("#overview-count").textContent = total ? `${total} 页` : "";
  if (!total) {
    grid.innerHTML = '<div class="overview-empty">暂无可预览页面</div>';
    return;
  }
  grid.innerHTML = Array.from({ length: total }, (_, i) => {
    const n = i + 1;
    const ready = ed.rendered.has(n);
    return `<button type="button" class="overview-card${ready ? "" : " pending"}" data-n="${n}" ${ready ? "" : "disabled"}>
      <span class="overview-no">${pad2(n)}</span>
      ${ready ? `<img src="${fileUrl(n)}" alt="第 ${n} 页预览">` : '<span class="overview-wait">未渲染</span>'}
    </button>`;
  }).join("");
}

function openOverviewModal() {
  renderOverviewGrid();
  $("#overview-modal").hidden = false;
}

function closeOverviewModal() {
  $("#overview-modal").hidden = true;
}

/* ----- 胶片条 ----- */
function buildFilmstrip() {
  const fs = $("#filmstrip");
  fs.innerHTML = "";
  ed.phLines = {};                              // 帧重建,占位帧微日志状态清零
  if (ed.kind === "dynamic") {
    const of = document.createElement("div");
    of.className = "frame frame-outline"; of.dataset.n = 0;
    of.innerHTML = `<span class="no">00</span>
      <div class="ocard"><span class="oc-title">过程</span><span class="oc-sub">PROCESS</span></div>`;
    of.onclick = () => setWorkspaceView("workflow");
    fs.appendChild(of);
  }
  for (let n = 1; n <= ed.total; n++) {
    const f = document.createElement("div");
    f.className = "frame"; f.dataset.n = n;
    f.innerHTML = `<span class="no">${pad2(n)}</span>
      <div class="ph"><i class="dots tl"><b></b><b></b><b></b></i><div class="ph-log"></div></div>`;
    f.onclick = () => select(n, { byUser: true });
    fs.appendChild(f);
    if (ed.rendered.has(n)) fillFrame(n);
  }
  if (ed.sel != null) markSel();
}

function wantUrl(n) { return new URL(fileUrl(n), location.origin).href; }

function fillFrame(n) {
  const f = $(`#filmstrip .frame[data-n="${n}"]`);
  if (!f) return;
  const img = document.createElement("img");
  // 注意:不能用 loading="lazy" —— 未挂载进 DOM 的 lazy 图片不会发起请求,onload 永远不触发。
  img.src = fileUrl(n);
  img.onload = () => {
    if (img.src !== wantUrl(n)) return;        // 加载期间又重渲了:丢弃过期图
    const ph = f.querySelector(".ph");
    const cur = f.querySelector("img");
    if (ph) ph.replaceWith(img);
    else if (cur && cur.src !== img.src) cur.src = img.src;
  };
  img.onerror = () => {};
}

// 某页被设计师自检重渲覆盖:刷新胶片条小图;若正选中,大画布预加载后无闪切换
function refreshFrame(n) {
  const url = fileUrl(n);
  const f = $(`#filmstrip .frame[data-n="${n}"] img`);
  if (f) f.src = url;
  else fillFrame(n);                            // 初图仍在加载:重走 fillFrame(其 onload 会校验最新 URL)
  if (ed.sel === n) {
    const big = $("#canvas-img");
    const pre = new Image();
    pre.onload = () => { if (ed.sel === n && pre.src === wantUrl(n)) big.src = pre.src; };
    pre.src = url;
  }
}

function markSel() {
  $$("#filmstrip .frame").forEach((f) => f.classList.toggle("sel", +f.dataset.n === ed.sel));
  const f = $(`#filmstrip .frame[data-n="${ed.sel}"]`);
  if (f) f.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function workflowRunStatus(key) {
  const timing = ed.agentTimings?.[key] || {};
  if (timing.execution_state) return timing.execution_state;
  const status = String(timing.status || "").toLowerCase();
  const failed = ["failed", "error", "rejected", "api_failed"].some((value) =>
    status.includes(value) || String(timing.exit_reason || "").toLowerCase().includes(value)
  );
  if (failed) return "failed";
  if (status === "complete") return "complete";
  if (status === "running") return "running";
  return Array.isArray(ed.feed?.[key]) && ed.feed[key].length
    ? (isStaticActiveStatus(ed.status) ? "running" : "complete")
    : "waiting";
}

function workflowStatusLabel(status) {
  return ({ complete: "已完成", running: "正在执行", failed: "执行失败", waiting: "等待输入",
    queued: "等待调度", resource_wait: "等待模型资源", interrupted: "已中断",
    unknown: "尚无状态记录", observed: "已有记录", skipped: "已跳过",
    not_started: "尚未启动", waiting_dependencies: "等待上游依赖",
    dependency_failed: "依赖失败 · 未启动", start_failed: "启动失败", cancelled: "已取消",
    no_messages: "暂无可展示消息" })[status] || "等待输入";
}

function workflowSlideOwners() {
  const runs = Array.isArray(ed.workflow?.runs) ? ed.workflow.runs : [];
  const keys = new Set([
    ...Object.values(ed.pageAgents || {}),
    ...Object.keys(ed.feed || {}).filter((key) => orchestrationAgentMeta(key).role === "slide"),
    ...runs.filter((run) => run.role === "slide").map((run) => run.key),
  ].filter(Boolean));
  return [...keys].map((key) => {
    const ownerRuns = runs.filter((run) => run.key === key && run.role === "slide");
    const pages = new Set(agentPages(key).map(Number));
    ownerRuns.forEach((run) => (run.pages || []).forEach((page) => pages.add(Number(page))));
    const orderedPages = [...pages].filter((page) => page > 0).sort((a, b) => a - b);
    const repairs = ownerRuns.filter((run) => run.owner_action === "reuse_owner").length;
    const groupId = ownerRuns.find((run) => run.group_id)?.group_id
      || String(key).replace(/^slide(?:_|-)group(?:_|-)/i, "");
    const grouped = /^slide(?:_|-)group(?:_|-)/i.test(String(key));
    return {
      key, pages: orderedPages, repairs, grouped, groupId,
      status: workflowRunStatus(key),
      ownerId: ownerRuns.find((run) => run.owner_id)?.owner_id || "",
    };
  }).sort((a, b) => (a.pages[0] || 999) - (b.pages[0] || 999));
}

function workflowNodeMarkup(node, { embedded = false } = {}) {
  const offset = ed.workflowNodeOffsets?.[node.id] || {};
  const dx = Number(offset.x) || 0;
  const dy = Number(offset.y) || 0;
  const x = node.x + dx;
  const y = node.y + dy;
  const pages = (node.pages || []).map((page) => `<button type="button" data-workflow-page="${page}" title="查看第 ${page} 页">P${pad2(page)}</button>`).join("");
  const thumbs = node.role === "slide"
    ? (node.pages || []).filter((page) => ed.rendered.has(page)).slice(0, 4)
      .map((page) => `<img src="${fileUrl(page)}" alt="P${pad2(page)}" loading="lazy">`).join("")
    : "";
  const repair = node.repairs ? `<b>修订回流 ×${node.repairs}</b>` : "";
  const traceSelected = ed.workflowSelectedNodeId ? ed.workflowSelectedNodeId === node.id
    : node.agentKey && node.agentKey === ed.workflowTraceAgent && (!node.segment || node.segment === ed.workflowTraceSegment);
  const detail = node.detail || "";
  const agentAttrs = node.agentKey || node.taskId
    ? ` data-agent-key="${escapeHtml(node.agentKey || "")}" data-workflow-task-id="${escapeHtml(node.taskId || "")}" data-workflow-segment="${escapeHtml(node.segment || "")}" role="button" tabindex="0" aria-label="选中 ${escapeHtml(node.title)}，右上角可查看迭代"`
    : "";
  return `<article class="workflow-node ${node.taskId ? "workflow-task-node " : ""}${escapeHtml(node.status)}${node.repairs ? " repaired" : ""}${traceSelected ? " trace-selected" : ""}"
      data-node-id="${escapeHtml(node.id)}" data-role="${escapeHtml(node.role)}"
      ${agentAttrs}
      data-base-x="${node.x}" data-base-y="${node.y}"
      ${embedded ? 'data-embedded-agent="true"' : `style="left:${x}px;top:${y}px;animation-delay:${node.delay || 0}ms"`}>
    <span class="workflow-anchor workflow-anchor-left" aria-hidden="true"></span>
    <span class="workflow-anchor workflow-anchor-right" aria-hidden="true"></span>
    <span class="workflow-anchor workflow-anchor-top" aria-hidden="true"></span>
    <span class="workflow-anchor workflow-anchor-bottom" aria-hidden="true"></span>
    ${node.agentKey || node.taskId ? `<button type="button" class="workflow-node-drilldown" data-workflow-drilldown="${escapeHtml(node.agentKey || "")}" aria-label="查看 ${escapeHtml(node.title)} 的迭代轨迹" title="查看画面、评审与修改过程">查看迭代</button>` : ""}
    <div class="workflow-node-kicker"><span><i></i>${escapeHtml(node.kicker)}</span><em>${escapeHtml(node.code || "")}</em></div>
    <strong title="${escapeHtml(node.title)}">${escapeHtml(node.title)}</strong>
    ${node.summary ? `<p class="workflow-task-goal" title="${escapeHtml(node.summary)}">${escapeHtml(node.summary)}</p>` : ""}
    ${node.status === "running" && node.activity ? "" : `<p title="${escapeHtml(detail)}">${escapeHtml(detail)}</p>`}
    ${node.activity ? `<div class="workflow-node-activity"><span>${node.status === "running" ? "当前动作" : "最近记录"}</span><b title="${escapeHtml(node.activity.title)}">${escapeHtml(node.activity.title)}</b>${node.activity.detail ? `<small title="${escapeHtml(node.activity.detail)}">${escapeHtml(node.activity.detail)}</small>` : ""}</div>` : ""}
    ${node.progress ? `<div class="workflow-node-progress">${escapeHtml(node.progress)}</div>` : ""}
    ${node.identity ? `<small class="workflow-task-identity" title="${escapeHtml(node.identity)}">${escapeHtml(node.identity)}</small>` : ""}
    ${pages ? `<div class="workflow-page-chips">${pages}</div>` : ""}
    ${thumbs ? `<div class="workflow-node-thumbs">${thumbs}</div>` : ""}
    <div class="workflow-node-foot"><i></i><span>${workflowStatusLabel(node.status)}</span>${repair}</div>
  </article>`;
}

function workflowArtifactUrl(file) {
  const rel = String(file?.rel || "").split("/").map(encodeURIComponent).join("/");
  if (!rel || !ed.id) return "#";
  const rawImage = rel.match(/^_trace\/raw\/blobs\/([a-f0-9]{64})$/);
  const path = rawImage ? `/trace-images/${rawImage[1]}` : `/files/${rel}`;
  return trajectoryMode
    ? trajectoryDeckApi(ed.id, path)
    : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}${path}`;
}

function workflowArtifactFiles() {
  const groups = ed.workflow?.artifacts?.groups || [];
  return groups.flatMap((group) => (group.files || []).map((file) => ({
    ...file,
    group: group.label || group.id || "Workspace",
  })));
}

function workflowArtifactRecord(rel, fallback = null) {
  const normalized = String(rel || "").replace(/^\.\//, "");
  const matched = workflowArtifactFiles().find((file) => file.rel === normalized);
  if (matched) return matched;
  if (!normalized) return null;
  const name = String(fallback?.name || normalized.split("/").at(-1) || "artifact");
  const extension = name.includes(".") ? name.split(".").at(-1).toLowerCase() : "";
  const kind = fallback?.kind || (/^(?:png|jpe?g|webp|gif|svg)$/i.test(extension) ? "image"
    : /^(?:md|markdown)$/i.test(extension) ? "markdown"
      : extension === "html" ? "html" : extension === "css" ? "style" : extension === "json" ? "data" : "file");
  return { rel: normalized, name, kind, size: Number(fallback?.size) || 0, updated_at: fallback?.updated_at || "", group: fallback?.group || "Workspace" };
}

function workflowArtifactLinkMarkup(rel, fallback = null) {
  const file = workflowArtifactRecord(rel, fallback);
  if (!file) return "";
  return `<a class="workflow-artifact-link kind-${escapeHtml(file.kind || "file")}" href="${workflowArtifactUrl(file)}"
      data-workflow-artifact-rel="${escapeHtml(file.rel)}" data-workflow-artifact-name="${escapeHtml(file.name || "artifact")}" data-workflow-artifact-kind="${escapeHtml(file.kind || "file")}">
    <i aria-hidden="true"></i><span><b>${escapeHtml(file.name || "artifact")}</b><small>${escapeHtml(file.rel)}</small></span><em>查看</em>
  </a>`;
}

function workflowArtifactRelsFromEvents(events, meta = null) {
  const files = workflowArtifactFiles();
  const rels = [];
  const add = (rel) => { if (rel && !rels.includes(rel)) rels.push(rel); };
  (events || []).forEach((event) => {
    if (event?.k !== "tool" || event.tool !== "write_file") return;
    const text = String(event.hint || event.s || "");
    files.forEach((file) => { if (text.includes(file.rel)) add(file.rel); });
    const direct = text.trim().replace(/^['"`]|['"`]$/g, "");
    if (/^(?:knowledge\.md|deck\.md|base\.css|speech\.md|present\.html|result\.json|(?:assets|slides|renders)\/[^\s]+)$/i.test(direct)) add(direct);
  });
  if (meta?.role === "slide") {
    const pages = meta.pages?.length ? meta.pages : (meta.page ? [meta.page] : []);
    pages.forEach((page) => {
      const number = pad2(page);
      [`slides/slide_${number}.html`, `renders/slide_${number}.png`].forEach((rel) => {
        if (files.some((file) => file.rel === rel)) add(rel);
      });
    });
  }
  return rels;
}

function workflowFileSize(value) {
  const size = Math.max(0, Number(value) || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function renderWorkflowArtifacts() {
  const tree = $("#workflow-file-tree");
  const count = $("#workflow-file-count");
  const state = $("#workflow-file-state");
  if (!tree) return;
  const artifacts = ed.workflow?.artifacts || {};
  const groups = Array.isArray(artifacts.groups) ? artifacts.groups : [];
  const fileCount = Number(artifacts.file_count) || groups.reduce((sum, group) => sum + (group.files?.length || 0), 0);
  const locked = artifacts.locked !== false;
  const signature = JSON.stringify([groups, locked]);
  if (tree.dataset.renderKey !== signature) {
    tree.dataset.renderKey = signature;
    tree.innerHTML = groups.length ? groups.map((group) => `<details class="workflow-file-group" open>
      <summary><span><i></i>${escapeHtml(group.label || group.id || "Files")}</span><em>${group.files?.length || 0}</em></summary>
      <div>${(group.files || []).map((file) => `<a class="workflow-file-item kind-${escapeHtml(file.kind || "file")}" href="${workflowArtifactUrl(file)}"
          data-workflow-artifact-rel="${escapeHtml(file.rel || "")}" data-workflow-artifact-name="${escapeHtml(file.name || "artifact")}" data-workflow-artifact-kind="${escapeHtml(file.kind || "file")}" title="${escapeHtml(file.rel || file.name || "")}">
        <i aria-hidden="true"></i><span><b>${escapeHtml(file.name || "file")}</b><small>${escapeHtml(workflowFileSize(file.size))}</small></span>
      </a>`).join("")}</div>
    </details>`).join("") : '<div class="workflow-panel-empty">文件落盘后会出现在这里。</div>';
  }
  tree.querySelectorAll("[data-workflow-artifact-rel]").forEach((item) => {
    item.classList.toggle("on", item.dataset.workflowArtifactRel === ed.workflowArtifactRel);
  });
  if (count) count.textContent = String(fileCount);
  if (state) {
    state.classList.toggle("ready", !locked);
    state.innerHTML = locked
      ? "<i></i><span>生成中只读 · 文件持续落盘</span>"
      : "<i></i><span>成稿已闭合 · 可预览文件或在右侧修改</span>";
  }
}

function workflowArtifactTextKind(file) {
  return ["markdown", "style", "data"].includes(String(file?.kind || ""))
    || /\.(?:md|markdown|txt|css|json|ya?ml|csv)$/i.test(String(file?.rel || ""));
}

function renderWorkflowArtifactText(file, text) {
  const stage = $("#workflow-artifact-stage");
  if (!stage || ed.workflowArtifactRel !== file.rel) return;
  const markdown = file.kind === "markdown" || /\.(?:md|markdown)$/i.test(file.rel);
  stage.className = `workflow-artifact-stage ${markdown ? "markdown" : "code"}`;
  stage.innerHTML = markdown
    ? `<article class="workflow-artifact-document agent-markdown">${agentMarkdownHtml(text)}</article>`
    : `<pre class="workflow-artifact-code"><code>${escapeHtml(text)}</code></pre>`;
}

async function renderWorkflowArtifactPane() {
  const pane = $("#workflow-artifact-pane");
  const stage = $("#workflow-artifact-stage");
  const title = $("#workflow-artifact-title");
  const path = $("#workflow-artifact-path");
  const meta = $("#workflow-artifact-meta");
  const open = $("#workflow-artifact-open");
  if (!pane || !stage) return;
  const file = workflowArtifactRecord(ed.workflowArtifactRel, ed.workflowArtifactFallback);
  if (!file) {
    stage.className = "workflow-artifact-stage";
    stage.innerHTML = '<div class="workflow-panel-empty">从左侧文件树或右侧会话选择一个产物。</div>';
    if (title) title.textContent = "产物详情";
    if (path) path.textContent = "";
    if (meta) meta.innerHTML = "";
    if (open) open.hidden = true;
    return;
  }
  const url = workflowArtifactUrl(file);
  if (title) title.textContent = file.name || "产物详情";
  if (path) path.textContent = file.rel;
  if (meta) meta.innerHTML = `<span>${escapeHtml(file.group || "Workspace")}</span><span>${escapeHtml(String(file.kind || "file").toUpperCase())}</span><span>${escapeHtml(workflowFileSize(file.size))}</span>${file.updated_at ? `<time>${escapeHtml(conversationTime(file.updated_at, { withDay: true })?.text || file.updated_at)}</time>` : ""}`;
  if (open) { open.hidden = false; open.href = url; }
  if (file.kind === "image" || /\.(?:png|jpe?g|webp|gif|svg)$/i.test(file.rel)) {
    stage.className = "workflow-artifact-stage image";
    stage.innerHTML = `<img src="${url}" alt="${escapeHtml(file.name || "产物预览")}">`;
    return;
  }
  if (file.kind === "html" || /\.html?$/i.test(file.rel)) {
    stage.className = "workflow-artifact-stage html";
    stage.innerHTML = `<iframe src="${url}" title="${escapeHtml(file.name || "HTML 产物预览")}"></iframe>`;
    return;
  }
  if (!workflowArtifactTextKind(file)) {
    stage.className = "workflow-artifact-stage unsupported";
    stage.innerHTML = `<div class="workflow-artifact-download"><i></i><strong>${escapeHtml(file.name || "产物文件")}</strong><p>该格式暂不支持内嵌预览，可打开原文件查看。</p><a href="${url}" target="_blank" rel="noopener">打开原文件 ↗</a></div>`;
    return;
  }
  const cacheKey = `${ed.id}:${file.rel}:${file.updated_at || file.size || 0}`;
  const cached = ed.workflowArtifactCache?.[cacheKey];
  if (typeof cached === "string") {
    renderWorkflowArtifactText(file, cached);
    return;
  }
  if (ed.workflowArtifactLoading === cacheKey) return;
  ed.workflowArtifactLoading = cacheKey;
  const request = ++ed.workflowArtifactRequest;
  stage.className = "workflow-artifact-stage loading";
  stage.innerHTML = '<div class="workflow-panel-empty">正在读取产物内容…</div>';
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("artifact unavailable");
    const text = await response.text();
    ed.workflowArtifactCache[cacheKey] = text;
    if (request === ed.workflowArtifactRequest) renderWorkflowArtifactText(file, text);
  } catch {
    if (request === ed.workflowArtifactRequest) {
      stage.className = "workflow-artifact-stage unsupported";
      stage.innerHTML = '<div class="workflow-panel-empty">这个产物暂时无法读取，可以尝试打开原文件。</div>';
    }
  } finally {
    if (ed.workflowArtifactLoading === cacheKey) ed.workflowArtifactLoading = "";
  }
}

function selectWorkflowArtifact(rel, fallback = null) {
  const file = workflowArtifactRecord(rel, fallback);
  if (!file) return;
  ed.workflowArtifactRel = file.rel;
  ed.workflowArtifactFallback = file;
  renderWorkflowArtifacts();
  setWorkflowPane("artifact");
  renderWorkflowArtifactPane();
}

function syncWorkflowArtifactSelection() {
  $$("#workflow-file-tree [data-workflow-artifact-rel], #workflow-message-list [data-workflow-artifact-rel]").forEach((item) => {
    item.classList.toggle("on", item.dataset.workflowArtifactRel === ed.workflowArtifactRel);
  });
}

function workflowAgentTraceUrl(agentKey) {
  const base = trajectoryMode
    ? trajectoryDeckApi(ed.id, `/agent-trace/${encodeURIComponent(agentKey)}`)
    : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}/agent-trace/${encodeURIComponent(agentKey)}`;
  const params = new URLSearchParams();
  if (ed.workflowTraceSegment && ed.workflowTraceSegmentPinned) params.set("segment", ed.workflowTraceSegment);
  params.set("source", ed.workflowTraceSource || "messages");
  if (ed.workflowOwnerAgent === agentKey) params.set("include_workflow", "true");
  return `${base}?${params}`;
}

function openWorkflowOwnerGraph(agentKey, { segment = "", taskId = "" } = {}) {
  cancelWorkflowAgentTraceRequest();
  const scroll = $("#workflow-scroll");
  if (!ed.workflowOwnerAgent && !ed.workflowOwnerTask) ed.workflowOverviewScroll = {top: scroll?.scrollTop || 0, left: scroll?.scrollLeft || 0};
  ed.workflowOwnerAgent = agentKey || "";
  ed.workflowOwnerTask = taskId;
  ed.workflowOwnerPage = null;
  ed.workflowOwnerRound = "";
  ed.workflowOwnerAllTools = !String(agentKey).startsWith("slide_");
  ed.workflowTraceLoadedAt = 0;
  ed.workflowTracePayload = null;
  if (!agentKey) setWorkflowConversationMode("history", {clearAgent: true});
  renderWorkflowOwnerGraph();
  if (agentKey) openWorkflowAgentTrace(agentKey, {segment});
  if (scroll) { scroll.scrollTop = 0; scroll.scrollLeft = 0; }
}

function closeWorkflowOwnerGraph() {
  const agentKey = ed.workflowOwnerAgent;
  const taskId = ed.workflowOwnerTask;
  ed.workflowOwnerAgent = "";
  ed.workflowOwnerTask = "";
  renderWorkflowOwnerGraph();
  renderWorkflowCanvas();
  const scroll = $("#workflow-scroll");
  if (scroll) {
    scroll.scrollTop = ed.workflowOverviewScroll?.top || 0;
    scroll.scrollLeft = ed.workflowOverviewScroll?.left || 0;
  }
  document.querySelector(taskId ? `.workflow-node[data-workflow-task-id="${CSS.escape(taskId)}"]`
    : `.workflow-node[data-agent-key="${CSS.escape(agentKey || "")}"]`)?.focus({preventScroll: true});
}

function workflowOwnerNodeMarkup(node, { compact = false } = {}) {
  const labels = {completed: "已完成", running: "进行中", failed: "失败", stale: "已过期", skipped: "已跳过"};
  const kinds = {edit: "修改", critic: "视觉评审", message: "Owner 消息", lifecycle: "执行状态", tool: "工具"};
  const title = node.kind === "critic" ? (node.status === "skipped" ? (node.title || "草稿检查") : "独立视觉 Critic") : node.kind === "edit" ? "源码修改" : node.title;
  const status = node.kind === "critic"
    ? ({completed: "反馈已返回", running: "等待公开反馈", failed: "评审执行未完成", stale: "像素已过期", skipped: "未调用外部 Critic"})[node.status] || "执行状态未记录"
    : labels[node.status] || node.status;
  const displayInfo = [...new Set([node.feedback, node.detail, node.observation, node.error, node.error_code, node.completion_note, node.next_required_action]
    .filter(Boolean).map(value => String(value).trim()))]
    .filter(Boolean).join("\n\n");
  const excerpt = displayInfo.slice(0, 180);
  const remaining = displayInfo.slice(180);
  const clock = String(node.timestamp || "").replace(/^.*T/, "").replace(/\.\d+Z$/, "Z");
  const meta = [clock, node.duration_ms != null ? `${(node.duration_ms / 1000).toFixed(1)}s` : "", node.critic_model,
    node.cache_hit ? "复用反馈 · 无新请求" : "", `#${node.sequence}`].filter(Boolean).join(" · ");
  return `<article class="workflow-owner-node kind-${escapeHtml(node.kind)} status-${escapeHtml(node.status)}${compact ? " compact" : ""}" data-owner-node="${escapeHtml(node.id)}">
    <div class="workflow-owner-node-top"><span class="workflow-owner-kind">${escapeHtml(kinds[node.kind] || "画面")}</span><span class="workflow-owner-status">${escapeHtml(status)}</span></div>
    <strong>${escapeHtml(title)}</strong><small>${escapeHtml(meta)}</small>
    ${excerpt ? `<p class="workflow-owner-excerpt">${escapeHtml(excerpt)}</p>` : ""}
    <details data-node-id="${escapeHtml(node.id)}"><summary>${remaining ? "展开剩余内容与执行信息" : "执行信息"}</summary>${remaining ? `<pre>${escapeHtml(remaining)}</pre>` : ""}<small>${node.kind === "critic" ? "inspect " : ""}执行段 ${escapeHtml(node.segment_id || "未记录")} · 回合 ${Number(node.turn) || 0}${node.inspection_id ? ` · 检查 ${escapeHtml(node.inspection_id)}` : ""}${node.png_sha256 ? ` · PNG ${escapeHtml(node.png_sha256)}` : ""}${node.rubric_version ? ` · Rubric ${escapeHtml(node.rubric_version)}` : ""}</small>${node.source_refs && Object.keys(node.source_refs).length ? `<pre>${escapeHtml(JSON.stringify(node.source_refs))}</pre>` : ""}</details>
    <footer>${node.render_id ? `<button type="button" data-owner-render="${escapeHtml(node.render_id)}">对应画面 ↗</button>` : ""}<button type="button" data-owner-trace="${escapeHtml(node.segment_id)}" data-owner-tool="${escapeHtml(node.tool_use_id || "")}">${node.kind === "critic" ? "查看 inspect 回执" : "查看 Trace"} ↗</button></footer>
  </article>`;
}

function workflowOwnerReadState(payload, graph, nodes) {
  if (payload?.load_error) return {kind: "error", title: "执行轨迹读取失败", message: payload.content, retry: true};
  if (payload?.refresh_error && !graph) return {kind: "error", title: "执行轨迹更新失败", message: payload.refresh_error, retry: true};
  if (!payload) return {kind: "loading", title: ed.workflowTraceLoading ? "正在加载执行轨迹" : "准备读取执行轨迹", message: "读取事件与对应图片版本，不会把当前图片替代历史版本。"};
  if (!Object.hasOwn(payload, "workflow")) return {kind: "unsupported", title: "服务尚未更新", message: "已读到 Agent Trace，但运行中的 WebUI 接口尚未返回下钻数据。请完成 WebUI 发布并重启服务后重试；这不代表 Agent 没有执行事件。", retry: true};
  if (!graph || !Array.isArray(graph.nodes)) return {kind: "error", title: "下钻数据格式异常", message: "接口未返回有效的事件列表，无法展示迭代过程。", retry: true};
  if (nodes.length) return null;
  if (graph.nodes.length) return {kind: "filtered", title: "当前筛选没有记录", message: "试试其他页面，或勾选“其他工具”查看全部动作。"};
  const kind = payload.empty_state?.kind || "empty";
  const titles = {not_started: "Agent 尚未启动", waiting_dependencies: "正在等待上游依赖", dependency_failed: "依赖失败，Agent 未启动", start_failed: "Agent 启动失败", cancelled: "任务已取消"};
  return {kind, title: titles[kind] || "尚无执行事件", message: payload.empty_state?.message || "本次运行还没有记录可展示的执行事件；后续落盘后会自动更新。"};
}

function workflowOwnerPageNodes(graph, page) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  if (page == null) return nodes;
  const renders = new Map(nodes.filter(node => node.kind === "render").map(node => [node.id, node]));
  return nodes.flatMap(node => {
    let pages = [...new Set([...(node.pages || []), ...(node.images || []).map(img => img.page)]
      .map(Number).filter(p => Number.isInteger(p) && p > 0))];
    if (node.kind === "critic") {
      const images = (renders.get(node.render_id)?.images || []).filter(img => img.sha256 === node.png_sha256);
      if (images.length) pages = [...new Set(images.map(img => Number(img.page)).filter(p => Number.isInteger(p) && p > 0))];
    }
    // 组级消息不按相邻渲染猜页码；多页渲染只投影当前页，Critic 按实际像素定位。
    if (page === 0) return (node.kind === "render" ? !pages.length : pages.length !== 1) ? [node] : [];
    if (node.kind === "render") return pages.includes(page)
      ? [{...node, images: (node.images || []).filter(img => Number(img.page) === page)}] : [];
    return pages.length === 1 && pages[0] === page ? [node] : [];
  });
}

function renderWorkflowOwnerGraph() {
  const panel = $("#workflow-owner-graph");
  if (!panel) return;
  const active = !!(ed.workflowOwnerAgent || ed.workflowOwnerTask);
  panel.hidden = !active;
  const stage = $("#workflow-stage");
  if (stage) stage.hidden = active;
  const overview = $("#workflow-overview");
  if (overview) overview.hidden = active;
  syncWorkflowLayoutReset();
  if (!active) return;
  if (!ed.workflowOwnerAgent) {
    const task = workflowTaskGraph(ed.muralRuntime?.tasks || [], ed.agentTimings || {}, ed.feed || {}, ed.status)
      .nodes.find(node => node.taskId === ed.workflowOwnerTask);
    if (task?.agentKey) {
      ed.workflowOwnerAgent = task.agentKey;
      openWorkflowAgentTrace(task.agentKey, {segment: task.segment});
    } else {
      $("#workflow-owner-title").textContent = task?.title || "任务执行过程";
      $("#workflow-owner-note").textContent = task ? workflowStatusLabel(task.status) : "暂未获取任务记录";
      $("#workflow-owner-pages").innerHTML = "";
      $("#workflow-owner-pages").hidden = true;
      const host = $("#workflow-owner-nodes");
      delete host.dataset.signature;
      host.innerHTML = `<div class="workflow-owner-empty" role="status"><h3>${escapeHtml(task ? workflowStatusLabel(task.status) : "暂未获取任务记录")}</h3><p>${escapeHtml(task?.detail || "等待下一次任务状态更新。")}</p><p>尚未获得此任务的 Owner 轨迹入口；入口建立后会自动加载，不代用其他 Agent 的轨迹。</p></div>`;
      return;
    }
  }
  const payload = ed.workflowTraceAgent === ed.workflowOwnerAgent ? ed.workflowTracePayload : null;
  const graph = payload?.workflow;
  const role = ({slide: "Slide Owner", image: "Image Owner", research: "Research Owner", material: "Material Owner", orchestrator: "Orchestrator"})[payload?.agent?.role] || payload?.agent?.role || "Agent";
  $("#workflow-owner-title").textContent = `${role} · ${(graph?.pages || []).map(p => `P${p}`).join(" / ") || "执行轨迹"}`;
  $("#workflow-owner-title").title = payload?.agent?.label || ed.workflowOwnerAgent;
  const pages = (graph?.pages || []).map(Number).filter(p => Number.isInteger(p) && p > 0).sort((a, b) => a - b);
  const paged = payload?.agent?.role === "slide" && pages.length > 0;
  if (paged && ed.workflowOwnerPage !== 0 && !pages.includes(ed.workflowOwnerPage)) ed.workflowOwnerPage = pages[0];
  const selectedPage = paged ? ed.workflowOwnerPage : null;
  const pageLabel = paged ? selectedPage ? `P${selectedPage}` : "组级记录" : "";
  const pageNav = $("#workflow-owner-pages");
  const pageButtons = paged ? [...pages, 0].map(p => `<button type="button" data-owner-page="${p}" aria-pressed="${selectedPage === p}"${selectedPage === p ? ' class="on"' : ""}>${p ? `P${p}` : "组级记录"}</button>`).join("") : "";
  if (pageNav.innerHTML !== pageButtons) pageNav.innerHTML = pageButtons;
  pageNav.hidden = !paged;
  $("#workflow-owner-all-tools").checked = !!ed.workflowOwnerAllTools;
  const nodes = workflowOwnerPageNodes(graph, selectedPage)
    .filter(node => ed.workflowOwnerAllTools || node.kind !== "tool" || node.status === "failed");
  const readState = workflowOwnerReadState(payload, graph, nodes);
  const renders = nodes.filter(node => node.kind === "render");
  const reviews = nodes.filter(node => node.kind === "critic");
  const edits = nodes.filter(node => node.kind === "edit");
  $("#workflow-owner-note").textContent = [pageLabel, readState ? readState.title : `${renders.length} 次画面记录 · ${reviews.length} 份外部反馈记录 · ${edits.length} 次源码修改`].filter(Boolean).join(" · ");
  const host = $("#workflow-owner-nodes");
  const signature = JSON.stringify([selectedPage, nodes, readState, payload?.refresh_error, ed.workflowOwnerRound]);
  if (host.dataset.signature === signature) return;
  host.dataset.signature = signature;
  const opened = new Set([...host.querySelectorAll("details[open]")].map(item => item.dataset.nodeId));
  if (readState) {
    host.innerHTML = `<div class="workflow-owner-empty state-${escapeHtml(readState.kind)}" role="status"><span class="workflow-owner-empty-icon" aria-hidden="true">${readState.kind === "loading" ? "◌" : "◇"}</span><h3>${escapeHtml(readState.title)}</h3><p>${escapeHtml(readState.message || "")}</p><div>${readState.retry ? '<button type="button" data-owner-retry>重新读取</button>' : ""}${payload ? '<button type="button" data-owner-trace="" data-owner-tool="">查看已有 Trace ↗</button>' : ""}</div></div>`;
    return;
  }
  const warning = payload?.refresh_error ? `<div class="workflow-owner-refresh-error" role="status">更新失败，保留已加载的记录。${escapeHtml(payload.refresh_error)} <button type="button" data-owner-retry>重试</button></div>` : "";
  if (!renders.length) {
    host.innerHTML = warning + `${paged && selectedPage === 0 ? '<p class="workflow-owner-caption">跨页操作、组级消息和未标明页码的记录集中展示于此，不归入任何单页版本。</p>' : ""}<div class="workflow-owner-timeline">${nodes.map(node => workflowOwnerNodeMarkup(node, {compact: true})).join("")}</div>`;
  } else {
    const index = Math.max(0, renders.findIndex(node => node.id === ed.workflowOwnerRound));
    const current = renders[index], next = renders[index + 1];
    const relatedReviews = reviews.filter(node => node.render_id === current.id);
    const during = nodes.filter(node => node.sequence > current.sequence && (!next || node.sequence < next.sequence) && node.kind !== "critic");
    const changes = during.filter(node => node.kind === "edit");
    const before = nodes.filter(node => node.sequence < current.sequence && node.kind !== "critic" && (index === 0 || node.sequence > renders[index - 1].sequence));
    const rail = renders.map((node, i) => {
      const img = node.images?.[0];
      const review = reviews.filter(review => review.render_id === node.id).at(-1);
      const reviewLabel = ({completed: "外部反馈已返回", running: "等待外部反馈", failed: "外部评审未完成", stale: "评审像素已过期", skipped: "草稿检查 · 未调用外部 Critic"})[review?.status];
      const label = `${pageLabel ? `${pageLabel} · ` : ""}第 ${i + 1} 次画面记录`;
      return `<button type="button" class="workflow-owner-round${i === index ? " on" : ""}" data-owner-round="${escapeHtml(node.id)}" aria-pressed="${i === index}" aria-label="查看${label}"><span><b>${escapeHtml(pageLabel || String(i + 1).padStart(2, "0"))}</b><em>${img ? `V${img.version}` : node.status === "running" ? "进行中" : "未留存"}</em></span>${img?.rel ? `<img loading="lazy" src="${workflowArtifactUrl({rel: img.rel})}" alt="${label}缩略图">` : '<i class="workflow-owner-round-placeholder">◇</i>'}<small>${escapeHtml(reviewLabel || (node.status === "failed" ? "执行失败" : "画面记录"))}</small></button>`;
    }).join("");
    const images = (current.images || []).map(img => `<figure>${img.rel ? `<a href="${workflowArtifactUrl({rel: img.rel})}" target="_blank" rel="noopener"><img src="${workflowArtifactUrl({rel: img.rel})}" alt="P${Number(img.page) || "?"} · 画面 V${Number(img.version)}"></a>` : '<div class="workflow-owner-missing">该像素版本尚未留存或 hash 不匹配<br>不会以当前截图替代</div>'}<figcaption>P${Number(img.page) || "?"} · V${Number(img.version)}<code title="${escapeHtml(img.sha256)}">${escapeHtml(String(img.sha256).slice(0, 12))}</code></figcaption></figure>`).join("");
    host.innerHTML = warning + `<nav class="workflow-owner-rounds" aria-label="画面迭代版本">${rail}</nav><div class="workflow-owner-stage"><section class="workflow-owner-canvas">
      <div class="workflow-owner-section-title"><span><b>01</b> ${pageLabel ? `${pageLabel} · ` : ""}画面 ${String(index + 1).padStart(2, "0")}${current.images?.[0] ? ` · V${current.images[0].version}` : ""}</span><div><button type="button" data-owner-round="${escapeHtml(renders[index - 1]?.id || "")}" ${index === 0 ? "disabled" : ""} aria-label="上一个画面">←</button><button type="button" data-owner-round="${escapeHtml(next?.id || "")}" ${!next ? "disabled" : ""} aria-label="下一个画面">→</button></div></div>
      <div class="workflow-owner-frame workflow-owner-node kind-render status-${escapeHtml(current.status)}" data-owner-node="${escapeHtml(current.id)}">${images || `<div class="workflow-owner-missing">${current.status === "running" ? "渲染 / 看图进行中，等待回执…" : "本条记录未包含可展示的截图"}</div>`}</div>
      ${current.pixel_unchanged ? '<p class="workflow-owner-warning">像素未改变</p>' : ""}${current.inspection_cached ? '<p class="workflow-owner-caption">复用已有检查回执</p>' : ""}
      ${before.length ? `<details class="workflow-owner-more" data-node-id="before-${escapeHtml(current.id)}"><summary>生成前的操作 <span>${before.length}</span></summary>${before.map(node => workflowOwnerNodeMarkup(node, {compact: true})).join("")}</details>` : ""}
    </section><section class="workflow-owner-checks"><div class="workflow-owner-section-title"><span><b>02</b> 检查请求与结果</span><small>同版像素 · 两条独立检查链路</small></div>
      <div class="workflow-owner-check-columns"><section class="workflow-owner-self-check"><h3>Owner 原生看图</h3>
        <p class="workflow-owner-caption">inspect 将当前截图与外部反馈交回同一个 Owner 的下一轮，由它核实并决定后续动作；工具成功不代表视觉通过。没有真实后续消息时不生成自评。</p>
        ${workflowOwnerNodeMarkup({...current, id: `${current.id}-receipt`, kind: "tool", title: current.title.includes("Owner 看图") ? "inspect 请求与截图回执" : "渲染请求与工具回执"}, {compact: true})}
      </section><section class="workflow-owner-review"><h3>外部 Critic 检查</h3>
        <p class="workflow-owner-caption">final/blocking_recheck 会触发独立会话检查同版截图；draft 或素材筛查只记录检查范围，不代表终稿通过或素材已用上。</p>
        ${relatedReviews.length ? relatedReviews.map(node => workflowOwnerNodeMarkup(node)).join("") : '<div class="workflow-owner-quiet">此版本尚无外部 Critic 记录，不推断通过、失败或是否启用。</div>'}
      </section></div>
    </section><section class="workflow-owner-changes"><div class="workflow-owner-section-title"><span><b>03</b> 后续流程</span><small>${changes.length} 次修改 · 按实际事件顺序</small></div>
      <p class="workflow-owner-caption">展示本${selectedPage ? "页" : "次"}画面请求之后、下一次${selectedPage ? "本页" : ""}画面请求之前的动作；顺序不表示这些修改都由 Critic 触发。${paged && selectedPage ? "跨页操作与未标明页码的消息请查看组级记录。" : ""}</p>
      <div class="workflow-owner-timeline">${during.map(node => workflowOwnerNodeMarkup(node, {compact: true})).join("") || '<div class="workflow-owner-quiet">此区间尚无后续动作记录。</div>'}</div>
      ${next ? `<button type="button" class="workflow-owner-next" data-owner-round="${escapeHtml(next.id)}">查看下一个画面 <span>→</span></button>` : '<p class="workflow-owner-caption">已到最后一条画面记录</p>'}
    </section></div><p class="workflow-owner-footnote">${escapeHtml(graph.note || "按实际事件顺序展示；评审仅对应其检查的像素版本。")}</p>`;
  }
  host.querySelectorAll("details[data-node-id]").forEach(item => { if (opened.has(item.dataset.nodeId)) item.open = true; });
}

function bindWorkflowOwnerGraph() {
  const panel = $("#workflow-owner-graph");
  if (!panel || panel.dataset.bound) return;
  panel.dataset.bound = "1";
  $("#workflow-owner-back").addEventListener("click", closeWorkflowOwnerGraph);
  $("#workflow-owner-all-tools").addEventListener("change", event => {
    ed.workflowOwnerAllTools = event.target.checked; renderWorkflowOwnerGraph();
  });
  panel.addEventListener("click", event => {
    const page = event.target.closest("[data-owner-page]");
    if (page) { ed.workflowOwnerPage = Number(page.dataset.ownerPage); ed.workflowOwnerRound = ""; renderWorkflowOwnerGraph(); return; }
    if (event.target.closest("[data-owner-retry]")) { loadWorkflowAgentTrace({force: true}); return; }
    const round = event.target.closest("[data-owner-round]");
    if (round && !round.disabled) { ed.workflowOwnerRound = round.dataset.ownerRound; renderWorkflowOwnerGraph(); return; }
    const render = event.target.closest("[data-owner-render]");
    if (render) {
      const graph = ed.workflowTracePayload?.workflow;
      const target = graph?.nodes?.find(node => node.id === render.dataset.ownerRender);
      if (target && ed.workflowTracePayload?.agent?.role === "slide"
        && !workflowOwnerPageNodes(graph, ed.workflowOwnerPage).some(node => node.id === target.id)) {
        ed.workflowOwnerPage = target.pages?.[0] || target.images?.[0]?.page || 0;
      }
      ed.workflowOwnerRound = render.dataset.ownerRender; renderWorkflowOwnerGraph();
      panel.querySelector(`[data-owner-node="${CSS.escape(render.dataset.ownerRender)}"]`)?.scrollIntoView({block: "center", behavior: "smooth"});
    }
    const trace = event.target.closest("[data-owner-trace]");
    if (trace) {
      const agentKey = ed.workflowOwnerAgent;
      ed.workflowTraceSource = "messages";
      openWorkflowAgentTrace(agentKey, {segment: trace.dataset.ownerTrace}).then(() => {
        if (ed.workflowTraceAgent !== agentKey || !trace.dataset.ownerTool) return;
        const call = $("#workflow-trace-messages")?.querySelector(`[data-tool-call-id="${CSS.escape(trace.dataset.ownerTool)}"]`);
        if (call) { call.open = true; call.scrollIntoView({block: "center", behavior: "smooth"}); }
      });
    }
  });
}

function workflowTraceByteLabel(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function workflowTraceSegmentLabel(segment, index) {
  const action = String(segment?.owner_action || "").toLowerCase();
  const prefix = action === "reuse_owner" ? "同一 Agent 续接" : index === 0 ? "初次执行" : "执行段";
  const id = segment?.segment_id;
  return id === null || id === undefined || id === "" ? prefix : `${prefix} · Segment ${id}`;
}

function workflowTraceClip(value, limit = 1800) {
  const text = String(value || "").replace(/\r\n?/g, "\n").trim();
  return text.length > limit ? `${text.slice(0, limit).trimEnd()}\n\n… 内容较长，已在可读视图中截断。` : text;
}

function workflowTraceMessageRecords() {
  const raw = String(ed.workflowTracePayload?.content || "").trim();
  if (!raw) return { records: [], error: "" };
  try {
    const parsed = JSON.parse(raw);
    const records = Array.isArray(parsed)
      ? parsed
      : Array.isArray(parsed?.messages)
        ? parsed.messages
        : Array.isArray(parsed?.items)
          ? parsed.items
          : [];
    return records.length
      ? { records, error: "" }
      : { records: [], error: "这个文件中没有可显示的消息记录。" };
  } catch {
    return { records: [], error: raw.startsWith("Agent Trace 读取失败") ? raw : "消息文件不是可读的标准消息列表。" };
  }
}

function workflowTraceMessageTime(index) {
  const value = ed.workflowTracePayload?.message_times?.[index];
  const time = conversationTime(value);
  const full = conversationTime(value, { withDay: true });
  return time && full
    ? `<time datetime="${escapeHtml(time.iso)}" title="${escapeHtml(full.text)}">${escapeHtml(time.text)}</time>`
    : "";
}

function workflowTraceHighlightMatches(container, query) {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!container || !normalized) {
    if (container) {
      container.dataset.searchCount = "0";
      container.dataset.searchIndex = "-1";
    }
    return 0;
  }
  let count = 0;
  container.querySelectorAll(".workflow-trace-message").forEach((message) => {
    const walker = document.createTreeWalker(message, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.toLocaleLowerCase().includes(normalized)) return NodeFilter.FILTER_REJECT;
        if (node.parentElement?.closest("mark, script, style")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const text = node.nodeValue;
      const lower = text.toLocaleLowerCase();
      const fragment = document.createDocumentFragment();
      let cursor = 0;
      let hit = lower.indexOf(normalized, cursor);
      while (hit !== -1) {
        if (hit > cursor) fragment.append(document.createTextNode(text.slice(cursor, hit)));
        const mark = document.createElement("mark");
        mark.className = "workflow-trace-search-hit";
        mark.dataset.searchMatch = String(count++);
        mark.textContent = text.slice(hit, hit + normalized.length);
        fragment.append(mark);
        cursor = hit + normalized.length;
        hit = lower.indexOf(normalized, cursor);
      }
      if (cursor < text.length) fragment.append(document.createTextNode(text.slice(cursor)));
      node.replaceWith(fragment);
    });
  });
  container.dataset.searchCount = String(count);
  container.dataset.searchIndex = count ? "0" : "-1";
  return count;
}

function focusWorkflowTraceSearchMatch(index, { scroll = true } = {}) {
  const list = $("#workflow-trace-messages");
  const matches = list ? Array.from(list.querySelectorAll("mark[data-search-match]")) : [];
  if (!matches.length) return;
  const normalized = ((Number(index) || 0) % matches.length + matches.length) % matches.length;
  matches.forEach((mark, position) => mark.classList.toggle("active", position === normalized));
  list.dataset.searchIndex = String(normalized);
  const selected = matches[normalized];
  selected.closest("details")?.setAttribute("open", "");
  if (scroll) selected.scrollIntoView({ block: "center", behavior: "smooth" });
}

function workflowTraceContentBlocks(message) {
  const content = message?.content;
  if (Array.isArray(content)) return content;
  if (typeof content === "string" && content.trim()) return [{ type: "text", text: content }];
  return [];
}

function workflowTraceResultParts(value) {
  const texts = [];
  let images = 0;
  const visit = (item) => {
    if (typeof item === "string") {
      if (item.trim()) texts.push(item.trim());
      return;
    }
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (!item || typeof item !== "object") return;
    const type = String(item.type || "").toLowerCase();
    if (type === "image" || item.image_url || item.source?.type === "base64") {
      images += 1;
      return;
    }
    if (typeof item.text === "string") texts.push(item.text.trim());
    else if (item.content !== undefined) visit(item.content);
  };
  visit(value);
  const text = texts.filter(Boolean).join("\n\n");
  // patch / inspect / render 返回结构化对象；可读轨迹必须保留这类结果。
  const structured = !text && value && typeof value === "object"
    ? workflowTraceSafeJson(value) : "";
  return { text, structured, images };
}

function workflowTraceSafeJson(value, limit = Infinity) {
  if (typeof value === "string") {
    const raw = value.trim();
    if (!raw) return "";
    if (!raw.startsWith("{") && !raw.startsWith("[")) return workflowTraceClip(value, limit);
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") value = parsed;
      else return workflowTraceClip(value, limit);
    } catch {
      return workflowTraceClip(value, limit);
    }
  }
  let text = "";
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value || "");
  }
  return workflowTraceClip(text, limit);
}

function workflowTraceToolSummary(input) {
  if (!input || typeof input !== "object") return "查看调用参数";
  const keys = ["path", "command", "image", "query", "url", "target", "task_name", "rel"];
  for (const key of keys) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return workflowTraceClip(value.split("\n")[0], 110);
  }
  const names = Object.keys(input);
  return names.length ? names.slice(0, 4).join(" · ") : "无参数";
}

function workflowTraceMessageKind(message) {
  const blocks = workflowTraceContentBlocks(message);
  if (String(message?.role || "").toLowerCase() === "assistant") return "agent";
  if (String(message?.role || "").toLowerCase() === "system") return "system";
  if (blocks.some((block) => block?.type === "tool_result")) return "tool";
  const text = blocks.map((block) => block?.text || "").join(" ").trim();
  return /^\[Harness\b/i.test(text) ? "system" : "user";
}

function workflowTraceMessageLabel(kind) {
  return ({ agent: "Agent", user: "任务输入", tool: "工具返回", system: "Harness" })[kind] || "消息";
}

function workflowTraceMessageMeta(blocks) {
  const labels = [];
  const toolCalls = blocks.filter((block) => block?.type === "tool_use").length;
  const toolResults = blocks.filter((block) => block?.type === "tool_result").length;
  if (blocks.some((block) => block?.type === "text")) labels.push("正文");
  if (blocks.some((block) => block?.type === "thinking" && block?.summary)) labels.push("分析摘要");
  if (blocks.some((block) => block?.type === "context_compaction")) labels.push("上下文压缩");
  if (toolCalls) labels.push(`${toolCalls} 个工具调用`);
  if (toolResults) labels.push(`${toolResults} 个结果`);
  return labels.join(" · ") || "上下文消息";
}

function workflowToolOutputState(toolId) {
  const cache = ed.workflowToolOutputs ||= {};
  return cache[`${ed.id}:${toolId}`] ||= {cursor: 0, rows: [], open: false, loading: false, complete: false};
}

function workflowToolOutputBody(state) {
  const rows = state.rows.map((row) => {
    const record = row.record;
    const prefix = `[${row.timestamp || ""} · 命令 ${row.command_index || 1} · ${row.stream}]`;
    return `${prefix}\n${row.text || (record ? JSON.stringify(record) : "")}`;
  }).join("\n");
  const truncated = state.rows.some((row) => row.record?.truncated);
  return `<small>${state.error ? escapeHtml(state.error) : state.loading ? "正在读取诊断…" : state.complete ? state.ok === false ? "工具执行失败" : "工具事务已结束" : "工具执行中 · 增量读取"}${truncated ? " · 输出已截断，见诊断元数据" : ""}${state.incomplete_tail ? " · 末尾记录未完整写入，已保留此前日志" : ""}</small><pre>${escapeHtml(rows || "尚无诊断输出")}</pre>${state.has_more ? '<button type="button" data-tool-output-more>继续读取</button>' : ""}`;
}

function workflowToolOutputMarkup(toolId) {
  if (trajectoryMode) return "";
  const state = workflowToolOutputState(toolId);
  return `<details class="workflow-tool-output" data-tool-output="${escapeHtml(toolId)}"${state.open ? " open" : ""}><summary data-tool-output-toggle>运行日志与逐项诊断 · stdout / stderr</summary><div class="workflow-tool-output-body">${workflowToolOutputBody(state)}</div></details>`;
}

async function loadWorkflowToolOutput(toolId, {force = false} = {}) {
  const state = workflowToolOutputState(toolId), deckId = ed.id;
  if (state.loading || !state.open) return;
  if (!force && Date.now() - (state.loadedAt || 0) < 2000) return;
  state.loading = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(deckId)}/tool-output/${encodeURIComponent(toolId)}?cursor=${state.cursor}`, {cache: "no-store", signal: controller.signal});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "诊断读取失败");
    if (ed.id !== deckId) return;
    state.rows.push(...payload.rows);
    state.cursor = payload.next_cursor;
    state.complete = payload.complete;
    state.ok = payload.ok;
    state.has_more = payload.has_more;
    state.incomplete_tail = payload.incomplete_tail;
    state.error = "";
  } catch (error) {
    state.error = `诊断读取失败：${error.message}（已读日志仍保留）`;
  } finally {
    clearTimeout(timer);
    state.loading = false;
    state.loadedAt = Date.now();
    if (ed.id === deckId) document.querySelectorAll(`[data-tool-output="${CSS.escape(toolId)}"] .workflow-tool-output-body`).forEach((body) => {
      body.innerHTML = workflowToolOutputBody(state);
    });
  }
}

function refreshWorkflowToolOutputs() {
  document.querySelectorAll("[data-tool-output][open]").forEach((panel) => {
    const state = workflowToolOutputState(panel.dataset.toolOutput);
    if (!state.complete && !state.has_more) loadWorkflowToolOutput(panel.dataset.toolOutput);
  });
}

function workflowTraceFailureMarkup() {
  const payload = ed.workflowTracePayload;
  const failure = payload?.segments?.find((segment) => segment.id === payload.selected_segment)?.failure;
  if (!failure) return "";
  const trigger = ({exact_repeat: "同一调用重复失败", coarse_repeat: "同类工具连续失败", no_progress: "重复操作无进展"})[failure.trigger];
  const title = trigger ? `${trigger} · ${failure.trigger_count}/${failure.threshold} 次，触发熔断` : failure.error_type || "执行失败";
  return `<details class="workflow-trace-failure"><summary>${escapeHtml(title)}</summary><pre>${escapeHtml(JSON.stringify(failure, null, 2))}</pre>${(failure.related_tool_use_ids || []).map((id, i) => `<button type="button" data-related-tool="${escapeHtml(id)}">定位调用 ${i + 1}</button>`).join("")}</details>`;
}

function workflowTraceBlockMarkup(block, toolNames) {
  if (!block || typeof block !== "object") return "";
  const type = String(block.type || "text");
  const preview = block.partial
    ? `<small class="workflow-trace-preview">${escapeHtml(livePreviewLabel({ partial: true, phase: block.interrupted ? "interrupted" : "" }))}</small>` : "";
  if (type === "text") {
    const text = String(block.text || "");
    return text ? `${preview}<div class="workflow-trace-message-text agent-markdown">${agentMarkdownHtml(text)}</div>` : "";
  }
  if (type === "thinking") {
    const summary = String(block.summary || "");
    if (!summary) return "";
    const label = livePreviewLabel({partial: block.partial, phase: block.interrupted ? "interrupted" : ""}, {thinking: true});
    return `<details class="workflow-trace-thinking orch-thinking-fold"><summary title="仅展示模型返回的思考摘要；已完成指本段摘要已返回，不代表整个任务结束。">${escapeHtml(label)}</summary><div class="agent-markdown">${agentMarkdownHtml(summary)}</div></details>`;
  }
  if (type === "context_compaction") {
    const phase = String(block.phase || "completed");
    const title = phase === "started" ? "开始压缩上下文" : phase === "failed" ? "上下文压缩未完成" : "上下文压缩完成";
    const before = Number(block.tokens_before || block.estimated_tokens || 0);
    const after = Number(block.tokens_after || 0);
    const budget = Number(block.input_budget || 0);
    const detail = phase === "started"
      ? [block.model || "模型", block.actual_max_tokens ? `输出上限 ${Number(block.actual_max_tokens).toLocaleString()} tokens` : ""].filter(Boolean).join(" · ")
      : phase === "failed"
        ? [block.error_type, block.error_summary, block.recoverable ? "可恢复，检查点已保留" : ""].filter(Boolean).join(" · ")
        : [before ? `压缩前 ${before.toLocaleString()}` : "", after ? `压缩后 ${after.toLocaleString()}` : "",
          budget ? `输入预算 ${budget.toLocaleString()}` : ""].filter(Boolean).join(" · ");
    return `<div class="workflow-trace-compaction phase-${escapeHtml(phase)}"><span aria-hidden="true">${phase === "failed" ? "!" : phase === "started" ? "…" : "✓"}</span><div><b>${escapeHtml(title)}</b><small>${escapeHtml(detail || "运行时上下文维护 · 非任务正文")}</small></div></div>`;
  }
  if (type === "tool_use") {
    const name = String(block.name || "tool");
    return `<details class="workflow-trace-tool-call" data-tool-call-id="${escapeHtml(block.id || "")}"><summary><i aria-hidden="true">↗</i><span><b>${escapeHtml(name)}</b><small>${escapeHtml(workflowTraceToolSummary(block.input))}</small></span><em>调用</em></summary><pre>${escapeHtml(workflowTraceSafeJson(block.input))}</pre></details>${block.has_diagnostics ? workflowToolOutputMarkup(String(block.id)) : ""}`;
  }
  if (type === "tool_result") {
    const result = workflowTraceResultParts(block.content);
    const name = toolNames.get(String(block.tool_use_id || "")) || "tool result";
    const diagnostic = block.content?.diagnostics;
    const failure = diagnostic?.records?.find((record) => record.status === "failed");
    const commandSummary = diagnostic ? `退出 ${block.content.returncode ?? "未启动"} · ${diagnostic.termination} · ${diagnostic.duration_ms}ms${failure ? ` · ${failure.asset_id || failure.operation}: ${failure.error_kind} ${failure.http_status || ""}` : ""}` : "";
    const readable = result.text || result.structured;
    const preview = commandSummary || workflowTraceClip(readable, 180).split("\n")[0] || (result.images ? `${result.images} 张图像已加入上下文` : "工具已返回结果");
    const detail = result.structured || result.text;
    return `<details class="workflow-trace-tool-result"><summary><i aria-hidden="true">↳</i><span><b>${escapeHtml(name)}</b><small>${escapeHtml(preview)}</small></span><em>${block.is_error ? "失败" : "结果"}</em></summary>${detail ? `<pre>${escapeHtml(detail)}</pre>` : ""}${result.images ? `<div class="workflow-trace-media-chip">${result.images} 张图像已加入原生多模态上下文</div>` : ""}</details>`;
  }
  if (type === "image") return '<div class="workflow-trace-media-chip">图像已加入原生多模态上下文</div>';
  return `<details class="workflow-trace-unknown"><summary>${escapeHtml(type)}</summary><pre>${escapeHtml(workflowTraceSafeJson(block))}</pre></details>`;
}

function workflowTraceReadableText(records) {
  const toolNames = new Map();
  records.forEach((message) => workflowTraceContentBlocks(message).forEach((block) => {
    if (block?.type === "tool_use" && block.id) toolNames.set(String(block.id), String(block.name || "tool"));
  }));
  return records.map((message, index) => {
    const kind = workflowTraceMessageKind(message);
    const lines = [`#${index + 1} ${workflowTraceMessageLabel(kind)}`];
    workflowTraceContentBlocks(message).forEach((block) => {
      if (block?.type === "text" && block.text) lines.push(String(block.text));
      else if (block?.type === "thinking" && block.summary) lines.push(`[分析摘要]\n${block.summary}`);
      else if (block?.type === "context_compaction") lines.push(`[上下文压缩 · ${block.phase || "completed"}]\n${workflowTraceSafeJson(block)}`);
      else if (block?.type === "tool_use") lines.push(`[调用 ${block.name || "tool"}]\n${workflowTraceSafeJson(block.input)}`);
      else if (block?.type === "tool_result") {
        const result = workflowTraceResultParts(block.content);
        lines.push(`[${toolNames.get(String(block.tool_use_id || "")) || "工具"} 返回]\n${result.text || result.structured || `${result.images} 张图像`}`);
      }
    });
    return lines.join("\n\n");
  }).join("\n\n---\n\n");
}

function workflowTraceConversationRows(records) {
  const calls = new Set(), results = new Map(), toolNames = new Map();
  records.forEach(message => workflowTraceContentBlocks(message).forEach(block => {
    if (block.type === "tool_use" && block.id) {
      calls.add(block.id);
      toolNames.set(block.id, block.name);
    }
    if (block.type === "tool_result" && block.tool_use_id) results.set(block.tool_use_id, block);
  }));
  const payload = ed.workflowTracePayload || {};
  const selected = payload.segments?.find(segment => segment.id === payload.selected_segment);
  const segmentId = selected?.segment_id || payload.selected_segment;
  const feed = (ed.feed?.[ed.workflowTraceAgent] || []).filter(event => !segmentId || event.segment_id === segmentId);
  const receipts = new Map(feed.filter(event => event.k === "tool_result").map(event => [event.tool_use_id, event]));
  const terminal = [...feed].reverse().find(event => ["agent_completed", "agent_failed"].includes(event.event_type));
  const active = isStaticActiveStatus(ed.status) && !terminal && !selected?.finished_at
    && !["completed", "failed", "succeeded", "interrupted"].includes(selected?.status);
  return records.map((message, index) => {
    const parts = workflowTraceContentBlocks(message).map((block, blockIndex) => {
      if (block.type === "tool_result" && calls.has(block.tool_use_id)) return "";
      if (block.type === "tool_use") {
        const receipt = results.get(block.id), event = receipts.get(block.id);
        const result = receipt ? {ok: !receipt.is_error, result: receipt.content,
          duration_ms: receipt.duration_ms ?? event?.duration_ms, error_code: receipt.error_code || event?.error_code} : null;
        const transaction = {id: block.id || `${index}-${blockIndex}`, tool: block.name,
          agent: payload.agent?.key || ed.workflowTraceAgent, use: {args: block.input}, result};
        const path = result?.ok && ["write_file", "patch"].includes(block.name) ? block.input?.path : "";
        const artifact = path ? workflowArtifactLinkMarkup(path) : "";
        const images = receipt ? workflowTraceResultParts(receipt.content).images : 0;
        return `<div class="orch-entry-copy orch-tool-message" data-tool-call-id="${escapeHtml(transaction.id)}">${liveToolTransactionMarkup(transaction, {compact: true, active})}
          ${block.name === "delegate_submit" ? workflowDelegationMarkup(transaction) : ""}
          ${artifact ? `<div class="orchestration-artifact-links">${artifact}</div>` : ""}
          ${images ? `<div class="workflow-trace-media-chip">${images} 张图像已加入原生多模态上下文</div>` : ""}
          ${block.has_diagnostics ? workflowToolOutputMarkup(transaction.id) : ""}</div>`;
      }
      if (message.role === "user" && block.type === "text") {
        let context;
        try { context = JSON.parse(block.text); } catch { /* 普通任务文本保持直接可读。 */ }
        if (context && typeof context === "object" && !Array.isArray(context)) {
          return `<details class="workflow-trace-context orch-thinking-fold" data-detail-key="context-${index}-${blockIndex}"><summary>任务上下文 · 展开查看</summary><pre>${escapeHtml(workflowTraceSafeJson(context))}</pre></details>`;
        }
      }
      return workflowTraceBlockMarkup(block, toolNames);
    }).filter(Boolean);
    return {index, role: message.role, html: parts.join("")};
  }).filter(row => row.html);
}

function renderWorkflowTraceMessages() {
  const list = $("#workflow-trace-messages");
  const input = $("#workflow-trace-search");
  if (!list) return;
  if (ed.workflowTraceLoading && !ed.workflowTracePayload) {
    delete list.dataset.renderKey;
    list.classList.add("loading");
    list.innerHTML = '<div class="workflow-trace-message-empty"><i></i><span>正在整理 Agent 消息…</span></div>';
    return;
  }
  list.classList.remove("loading");
  const empty = ed.workflowTracePayload?.empty_state;
  if (empty) {
    delete list.dataset.renderKey;
    const causes = ed.workflowTracePayload?.task_state?.dependency_causes || [];
    list.innerHTML = workflowTraceFailureMarkup() + `<div class="workflow-trace-message-empty task-state"><strong>${escapeHtml(workflowStatusLabel(empty.kind))}</strong><span>${escapeHtml(empty.message)}</span>${causes.map((cause) => cause.agent_key && cause.segment_id ? `<button type="button" data-dependency-agent="${escapeHtml(cause.agent_key)}" data-dependency-segment="${escapeHtml(cause.segment_id)}">查看 ${escapeHtml(cause.label)} 的失败执行段</button>` : `<small>${escapeHtml(cause.task_id)}</small>`).join("")}<small>调度状态说明，不是 Agent 消息。</small></div>`;
    return;
  }
  const raw = String(ed.workflowTracePayload?.content || "");
  if (!raw) {
    delete list.dataset.renderKey;
    list.innerHTML = `<div class="workflow-trace-message-empty"><span>${ed.workflowTraceAgent ? "当前执行段尚未写入消息。" : "点击协作图中的 Agent 卡片查看消息。"}</span></div>`;
    return;
  }
  const { records, error } = workflowTraceMessageRecords();
  if (error) {
    delete list.dataset.renderKey;
    list.innerHTML = `<div class="workflow-trace-message-empty error"><span>${escapeHtml(error)}</span></div>`;
    return;
  }
  const query = String(input?.value || "").trim();
  const hasDelegation = records.some(message => workflowTraceContentBlocks(message)
    .some(block => block.type === "tool_use" && block.name === "delegate_submit"));
  const key = JSON.stringify([
    ed.workflowTraceAgent,
    ed.status,
    ed.feed?.[ed.workflowTraceAgent]?.at(-1)?.seq,
    ed.workflowTracePayload?.updated_at,
    ed.workflowTracePayload?.selected_segment,
    raw.length,
    raw.slice(-80),
    workflowTraceFailureMarkup(),
    hasDelegation ? workflowDelegationStateKey() : "",
    query,
  ]);
  if (list.dataset.renderKey === key) return;
  list.dataset.renderKey = key;
  const scope = JSON.stringify([ed.workflowTraceAgent, ed.workflowTracePayload?.selected_segment]);
  if (list.dataset.conversationScope !== scope || !list.querySelector(".workflow-trace-conversation")) {
    list.dataset.conversationScope = scope;
    list.innerHTML = '<div class="workflow-trace-notice"></div><div class="workflow-trace-message-count"><span></span><small data-trace-search-summary>按执行顺序排列</small></div><div class="workflow-trace-conversation"></div>';
  }
  const notice = list.querySelector(".workflow-trace-notice"), failure = workflowTraceFailureMarkup();
  if (notice.innerHTML !== failure) notice.innerHTML = failure;
  list.querySelector(".workflow-trace-message-count > span").textContent = `${records.length} 条原始消息 · 工具请求与回执合并显示`;
  const lane = list.querySelector(".workflow-trace-conversation");
  const existing = new Map([...lane.children].map(card => [Number(card.dataset.messageIndex), card]));
  let minute = "";
  workflowTraceConversationRows(records).forEach((row, position) => {
    const card = existing.get(row.index) || document.createElement("article");
    const value = ed.workflowTracePayload?.message_times?.[row.index];
    const exact = conversationTime(value, {withDay: true});
    const time = conversationTime(value, {withDay: true, bucketMinutes: 5});
    const heading = time && time.groupKey !== minute ? `<time class="orch-time-group" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(exact?.text || time.text)}">${escapeHtml(time.text)}</time>` : "";
    if (time) minute = time.groupKey;
    const content = heading + row.html;
    card.className = `workflow-trace-message conversation-message role-${row.role}`;
    card.dataset.messageIndex = row.index;
    if (card._conversationHtml !== content || query || list.dataset.lastQuery) {
      const open = [...card.querySelectorAll("details")].map((detail, index) => detail.open ? index : -1);
      card.innerHTML = content;
      card.querySelectorAll("details").forEach((detail, index) => { if (open.includes(index)) detail.open = true; });
      card._conversationHtml = content;
    }
    existing.delete(row.index);
    if (lane.children[position] !== card) lane.insertBefore(card, lane.children[position] || null);
  });
  existing.forEach(card => card.remove());
  list.dataset.lastQuery = query;
  const matches = workflowTraceHighlightMatches(list, query);
  const summary = list.querySelector("[data-trace-search-summary]");
  if (summary) summary.textContent = query ? matches ? `${matches} 处匹配 · Enter 跳转` : "未找到匹配内容" : "按执行顺序排列";
  if (matches) focusWorkflowTraceSearchMatch(0);
}

function renderWorkflowTraceContent() {
  const source = ed.workflowTracePayload?.source || ed.workflowTraceSource || "messages";
  const readable = source === "messages";
  if (readable) renderWorkflowTraceMessages();
  else renderWorkflowTraceJson();
}

function renderWorkflowProviderAttempts() {
  const host = $("#workflow-provider-attempts");
  if (!host) return;
  host.hidden = ed.workflowTracePayload?.source !== "provider_attempts" || trajectoryMode;
  if (host.hidden) return;
  let events;
  try { events = JSON.parse(ed.workflowTracePayload.content); } catch { events = []; }
  const attempts = [...new Set(events.map(row => row.payload?.attempt_id).filter(Boolean))];
  const cache = ed.workflowProviderTrace ||= {};
  host.replaceChildren();
  for (const attempt of attempts) {
    for (const source of ["request", "response", "stream"]) {
      const key = `${ed.id}:${attempt}:${source}`;
      const state = cache[key] ||= {cursor: 0, content: "", open: false};
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const pre = document.createElement("pre");
      const more = document.createElement("button");
      summary.textContent = `${attempt} · ${source}（签名仅显示元数据）`;
      pre.textContent = state.content || state.error || "点击展开后读取";
      more.type = "button";
      more.textContent = source === "stream" ? "读取新增记录" : "继续读取";
      more.hidden = source !== "stream" && state.has_more === false;
      details.open = state.open;
      details.append(summary, pre, more);
      host.append(details);
      async function load() {
        if (state.loading) return;
        state.loading = true;
        more.disabled = true;
        const deckId = ed.id;
        try {
          const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${deckId}/provider-trace/${encodeURIComponent(attempt)}/${source}?cursor=${state.cursor}&limit=65536`);
          if (!response.ok) throw new Error(response.status === 404 ? "尚未记录；可稍后重试" : `读取失败 (${response.status})`);
          const value = await response.json();
          state.content += value.content;
          state.cursor = value.next_cursor;
          state.has_more = value.has_more;
          state.error = "";
        } catch (error) { state.error = error.message; }
        finally { state.loading = false; }
        if (ed.id === deckId) renderWorkflowProviderAttempts();
      }
      details.ontoggle = () => {
        const changed = state.open !== details.open;
        state.open = details.open;
        if (changed && state.open && !state.content) load();
      };
      more.onclick = load;
    }
  }
}

function renderWorkflowTraceJson() {
  const pre = $("#workflow-trace-json");
  const input = $("#workflow-trace-search");
  if (!pre) return;
  if (ed.workflowTraceLoading && !ed.workflowTracePayload) {
    pre.classList.add("loading");
    pre.textContent = "正在读取 Agent Trace…";
    return;
  }
  pre.classList.remove("loading");
  const raw = ed.workflowTracePayload?.empty_state?.message || String(ed.workflowTracePayload?.content || "");
  if (!raw) {
    pre.textContent = ed.workflowTraceAgent ? "当前执行段尚未写入这个 Trace 文件。" : "选择 Agent 后，可查看工具、配置、用量和交接等结构化信息。";
    return;
  }
  const query = String(input?.value || "").trim();
  const key = JSON.stringify([
    ed.workflowTracePayload?.updated_at,
    ed.workflowTracePayload?.source,
    ed.workflowTracePayload?.selected_segment,
    raw.length,
    raw.slice(-80),
    query,
  ]);
  if (pre.dataset.renderKey === key) return;
  pre.dataset.renderKey = key;
  if (!query) {
    pre.textContent = raw;
    return;
  }
  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`(${escapedQuery})`, "gi");
  pre.innerHTML = raw.split(pattern).map((part, index) =>
    index % 2 ? `<mark>${escapeHtml(part)}</mark>` : escapeHtml(part)
  ).join("");
  pre.querySelector("mark")?.scrollIntoView({ block: "center" });
}

function renderWorkflowAgentTrace() {
  renderWorkflowImageOutputs();
  const payload = ed.workflowTracePayload;
  const completeTrace = payload?.selected_segment === "__all__";
  const selected = completeTrace ? payload?.segments?.at(-1) || null
    : payload?.segments?.find((segment) => segment.id === payload.selected_segment) || null;
  const source = payload?.source || ed.workflowTraceSource || "messages";
  const readable = source === "messages";
  const pendingTask = payload?.task_state?.trace_available === false;
  const followLatest = !!payload?.task_state || payload?.projected_from === "live-events.jsonl";
  const title = $("#workflow-trace-title");
  const role = $("#workflow-trace-role");
  const meta = $("#workflow-trace-meta");
  const segmentSelect = $("#workflow-trace-segment");
  const agentSelect = $("#workflow-trace-agent");
  const filename = $("#workflow-trace-filename");
  const updated = $("#workflow-trace-updated");
  const messageList = $("#workflow-trace-messages");
  const rawJson = $("#workflow-trace-json");
  const search = $("#workflow-trace-search");
  const copy = $("#workflow-trace-copy");
  const footerNote = $("#workflow-trace-footer-note");
  if (title) title.textContent = payload?.agent?.label || ed.workflowTraceAgent || "选择一个 Agent";
  if (agentSelect && payload?.agents) {
    const key = JSON.stringify(payload.agents);
    if (agentSelect.dataset.renderKey !== key) {
      agentSelect.dataset.renderKey = key;
      agentSelect.innerHTML = payload.agents.map(agent => `<option value="${escapeHtml(agent.key)}">${escapeHtml(
        `${agent.role}${agent.page ? ` · P${agent.page}` : ""} · ${agent.label}`)}</option>`).join("");
    }
    agentSelect.value = payload.agent?.key || ed.workflowTraceAgent;
  }
  if (role) role.textContent = String(payload?.agent?.role || "AGENT").toUpperCase() + (readable ? " 轨迹" : " TRACE");
  if (meta) {
    const details = [completeTrace ? `${payload?.segments?.length || 0} 个执行段 · 完整轨迹` : "",
      selected?.model, selected?.thinking_effort, selected?.owner_action === "reuse_owner" ? "复用 Owner" : ""].filter(Boolean);
    meta.textContent = payload?.empty_state ? workflowStatusLabel(payload.empty_state.kind) : details.join(" · ") || "执行消息与工具记录";
  }
  if (segmentSelect && payload?.segments) {
    const key = JSON.stringify([followLatest, pendingTask, payload.segments.map((segment) => [segment.id, segment.segment_id, segment.owner_action])]);
    if (segmentSelect.dataset.renderKey !== key) {
      segmentSelect.dataset.renderKey = key;
      segmentSelect.innerHTML = `<option value="__all__">完整轨迹（跨压缩 / 续接段）</option>`
        + (followLatest ? `<option value="">${pendingTask ? "当前任务（未启动）" : "跟随最新执行段"}</option>` : "") + payload.segments.map((segment, index) =>
        `<option value="${escapeHtml(segment.id)}">${escapeHtml(workflowTraceSegmentLabel(segment, index))}</option>`
      ).join("");
    }
    segmentSelect.value = ed.workflowTraceSegmentPinned ? ed.workflowTraceSegment
      : completeTrace ? "__all__" : followLatest ? "" : payload.selected_segment || "";
  }
  const available = new Set((selected?.sources || []).map((item) => item.id));
  $$("#workflow-trace-sources [data-trace-source]").forEach((button) => {
    const buttonSource = button.dataset.traceSource;
    button.classList.toggle("on", buttonSource === source);
    button.disabled = !!payload && !available.has(buttonSource);
  });
  if (messageList) messageList.hidden = !readable;
  if (rawJson) rawJson.hidden = readable;
  if (search) search.placeholder = readable ? "查找并定位…" : "查找结构化信息…";
  if (copy) copy.textContent = readable ? "复制消息" : "复制信息";
  if (copy) copy.disabled = !!payload?.empty_state;
  if (search) search.disabled = !!payload?.empty_state;
  if (footerNote) footerNote.textContent = payload?.empty_state
    ? "历史执行段仍可从上方选择；新任务启动后显示真实消息。" : readable
    ? completeTrace ? "该 Agent 的跨段完整消息 · 压缩与工具详情按序显示" : "可读信息 · 推理与工具详情按需展开"
    : completeTrace ? "该 Agent 的全部已记录事件 · 敏感凭证自动脱敏" : "结构化信息 · 敏感凭证自动脱敏";
  if (filename) filename.textContent = payload?.filename
    ? `${payload.filename} · ${workflowTraceByteLabel(payload.size)}` : payload?.empty_state ? "尚无 Agent Trace" : "Trace";
  if (updated) updated.textContent = payload?.updated_at
    ? (conversationTime(payload.updated_at, { withDay: true })?.text || "") : "";
  renderWorkflowTraceContent();
  renderWorkflowProviderAttempts();
}

function workflowDefaultTraceAgentKey() {
  const nodes = Array.from(document.querySelectorAll(".workflow-node[data-agent-key]"));
  const orchestrator = nodes.find((node) => node.dataset.role === "orchestrator");
  return orchestrator?.dataset.agentKey || nodes[0]?.dataset.agentKey || (ed.id ? "orch" : "");
}

function syncWorkflowConversationMode() {
  const aside = $(".workflow-conversation");
  const hasTrace = !!ed.workflowTraceAgent || !!workflowDefaultTraceAgentKey();
  const mode = ed.workflowConversationMode === "trace" && hasTrace ? "trace" : "history";
  ed.workflowConversationMode = mode;
  aside?.classList.toggle("trace-mode", mode === "trace");
  $("#workflow-agent-trace")?.toggleAttribute("hidden", mode !== "trace");
  const title = $("#workflow-conversation-title");
  const kicker = $("#workflow-conversation-kicker");
  if (title) title.textContent = mode === "trace" ? "Agent 完整轨迹" : "历史与消息";
  if (kicker) kicker.textContent = mode === "trace" ? "AGENT EXECUTION" : "HISTORY & MESSAGES";
  const tabs = $("#workflow-conversation-tabs");
  if (tabs) tabs.dataset.active = mode;
  tabs?.querySelectorAll("[data-workflow-conversation-mode]").forEach((button) => {
    const active = button.dataset.workflowConversationMode === mode;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
    if (button.dataset.workflowConversationMode === "trace") button.disabled = !hasTrace;
  });
  const shell = $(".workflow-studio-shell");
  shell?.classList.toggle("trace-expanded", mode === "trace" && !!ed.workflowTraceExpanded);
  $$(".workflow-node[data-agent-key]").forEach((node) => {
    node.classList.toggle("trace-selected", ed.workflowSelectedNodeId
      ? node.dataset.nodeId === ed.workflowSelectedNodeId
      : mode === "trace" && node.dataset.agentKey === ed.workflowTraceAgent
        && (!node.dataset.workflowSegment || node.dataset.workflowSegment === ed.workflowTraceSegment));
  });
}

function setWorkflowConversationMode(mode, { clearAgent = false } = {}) {
  if (clearAgent) {
    cancelWorkflowAgentTraceRequest();
    ed.workflowTraceAgent = "";
    ed.workflowTraceSegment = "";
    ed.workflowTraceSegmentPinned = false;
    ed.workflowTracePayload = null;
  }
  if (mode === "trace" && !ed.workflowTraceAgent) {
    ed.workflowTraceAgent = workflowDefaultTraceAgentKey();
    ed.workflowTraceSegment = "__all__";
    ed.workflowTraceSegmentPinned = true;
    ed.workflowTraceSource = "messages";
    ed.workflowTracePayload = null;
    ed.workflowTraceLoadedAt = 0;
  }
  ed.workflowConversationMode = mode === "trace" && ed.workflowTraceAgent ? "trace" : "history";
  syncWorkflowConversationMode();
  if (ed.workflowConversationMode === "history") {
    renderWorkflowConversation();
  } else {
    renderWorkflowAgentTrace();
    loadWorkflowAgentTrace({ force: !!ed.workflowTracePayload?.load_error });
  }
}

function cancelWorkflowAgentTraceRequest() {
  ed.workflowTraceAbortController?.abort();
  ed.workflowTraceAbortController = null;
  ed.workflowTraceLoading = false;
  ed.workflowTraceRequest = (Number(ed.workflowTraceRequest) || 0) + 1;
}

async function loadWorkflowAgentTrace({ force = false } = {}) {
  if (!ed.id || !ed.workflowTraceAgent) return;
  refreshWorkflowToolOutputs();
  if (ed.workflowTraceLoading && !force) return;
  const active = isStaticActiveStatus(ed.status);
  if (!force && ed.workflowTracePayload && (!active || Date.now() - (ed.workflowTraceLoadedAt || 0) < WORKFLOW_TRACE_REFRESH_MS)) return;
  if (force && ed.workflowTraceLoading) cancelWorkflowAgentTraceRequest();
  const deckId = ed.id;
  const agentKey = ed.workflowTraceAgent;
  const request = ++ed.workflowTraceRequest;
  const previousPayload = ed.workflowTracePayload;
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, WORKFLOW_TRACE_REQUEST_TIMEOUT_MS);
  ed.workflowTraceAbortController = controller;
  ed.workflowTraceLoading = true;
  renderWorkflowAgentTrace();
  if (ed.workflowOwnerAgent) renderWorkflowOwnerGraph();
  try {
    const response = await fetch(workflowAgentTraceUrl(agentKey), {
      cache: "no-store",
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "Agent Trace 读取失败");
    if (ed.id !== deckId || ed.workflowTraceAgent !== agentKey || request !== ed.workflowTraceRequest) return;
    ed.workflowTracePayload = payload;
    if (payload.selected_segment || !ed.workflowTraceSegmentPinned) {
      ed.workflowTraceSegment = payload.selected_segment || "";
    }
    ed.workflowTraceSource = payload.source || ed.workflowTraceSource || "messages";
    ed.workflowTraceLoadedAt = Date.now();
  } catch (error) {
    if (ed.id === deckId && ed.workflowTraceAgent === agentKey && request === ed.workflowTraceRequest) {
      if (controller.signal.aborted && !timedOut) return;
      const message = timedOut
        ? "Agent Trace 读取超时，请重新点击该 Agent 重试。"
        : `Agent Trace 读取失败：${error.message}`;
      ed.workflowTracePayload = previousPayload && !previousPayload.load_error ? {...previousPayload, refresh_error: message} : {
        content: message,
        source: ed.workflowTraceSource,
        segments: [],
        load_error: true,
      };
      ed.workflowTraceLoadedAt = Date.now();
    }
  } finally {
    clearTimeout(timeout);
    if (request === ed.workflowTraceRequest) {
      ed.workflowTraceAbortController = null;
      ed.workflowTraceLoading = false;
      renderWorkflowAgentTrace();
      refreshWorkflowToolOutputs();
      if (ed.workflowOwnerAgent) renderWorkflowOwnerGraph();
    }
  }
}

function openWorkflowAgentTrace(agentKey, { segment = "", taskId = "" } = {}) {
  const normalized = String(agentKey || "").trim();
  if (!normalized) return;
  const nodes = [...document.querySelectorAll(".workflow-node[data-agent-key]")].filter(node => node.dataset.agentKey === normalized);
  const selected = (segment && nodes.find(node => node.dataset.workflowSegment === segment))
    || (taskId && nodes.find(node => node.dataset.workflowTaskId === taskId))
    || (!segment && nodes.find(node => node.dataset.nodeId === ed.workflowSelectedNodeId))
    || nodes.find(node => !segment || !node.dataset.workflowSegment);
  ed.workflowSelectedNodeId = selected?.dataset.nodeId || (taskId && ed.muralRuntime?.tasks?.some(task => task.task_id === taskId) ? `task:${taskId}` : "");
  if (ed.workflowOwnerAgent && ed.workflowOwnerAgent !== normalized) {
    ed.workflowOwnerAgent = normalized;
    ed.workflowOwnerTask = "";
    ed.workflowOwnerPage = null;
  }
  const changed = ed.workflowTraceAgent !== normalized;
  const sourceChanged = ed.workflowTraceSource !== "messages";
  if (changed) {
    cancelWorkflowAgentTraceRequest();
    ed.workflowTraceAgent = normalized;
    ed.workflowTraceSegment = "__all__";
    ed.workflowTraceSegmentPinned = true;
    ed.workflowTraceSource = "messages";
    ed.workflowTracePayload = null;
    ed.workflowTraceLoadedAt = 0;
    const search = $("#workflow-trace-search");
    if (search) search.value = "";
  }
  ed.workflowConversationMode = "trace";
  if (sourceChanged) {
    ed.workflowTraceSource = "messages";
    ed.workflowTracePayload = null;
  }
  if (segment) {
    ed.workflowTraceSegment = segment;
    ed.workflowTraceSegmentPinned = true;
    ed.workflowTracePayload = null;
  }
  const state = ed.workflowPanelCollapsed ||= { files: false, conversation: false };
  const wasCollapsed = !!state.conversation;
  state.conversation = false;
  syncWorkflowPanelLayout({ remeasure: wasCollapsed });
  syncWorkflowConversationMode();
  if (selected && ed.workflowPane === "graph") selected.scrollIntoView({block: "nearest", inline: "center", behavior: "smooth"});
  renderWorkflowAgentTrace();
  return loadWorkflowAgentTrace({ force: changed || sourceChanged || !!segment || !!ed.workflowTracePayload?.load_error });
}

function bindWorkflowTraceControls() {
  const openDelegated = (event) => {
    if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return false;
    const card = event.target.closest('[data-embedded-agent="true"]');
    if (!card || window.getSelection()?.toString()) return false;
    const interactive = event.target.closest("a, summary, details, input, textarea, select");
    if (interactive) return false;
    if (ed.workspaceView && ed.workspaceView !== "workflow") setWorkspaceView("workflow");
    const page = event.target.closest("[data-workflow-page]");
    if (page) {
      if (event.type === "click") {
        ed.workflowPage = Number(page.dataset.workflowPage);
        setWorkflowPane("page");
        renderWorkflowPagePane();
      }
      return true;
    }
    if (event.target.closest("button") && event.type === "keydown") return true;
    event.preventDefault();
    ed.workflowSelectedNodeId = card.dataset.ownerNodeId || card.dataset.nodeId;
    setWorkflowPane("graph");
    openWorkflowOwnerGraph(card.dataset.agentKey || "", {taskId: card.dataset.workflowTaskId || ""});
    return true;
  };
  const preview = $("#agent-progress-console");
  if (preview && preview.dataset.ownerNavigationBound !== "1") {
    preview.dataset.ownerNavigationBound = "1";
    preview.addEventListener("click", openDelegated);
    preview.addEventListener("keydown", openDelegated);
  }
  const history = $("#workflow-message-list");
  if (history && history.dataset.agentNavigationBound !== "1") {
    history.dataset.agentNavigationBound = "1";
    const navigate = (event) => {
      if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
      if (openDelegated(event)) return;
      if (event.target.closest("a, button, summary, details, input, textarea, select, [contenteditable]")) return;
      if (window.getSelection()?.toString()) return;
      const card = event.target.closest("[data-message-agent]");
      if (!card) return;
      event.preventDefault();
      openWorkflowAgentTrace(card.dataset.messageAgent, {segment: card.dataset.messageSegment, taskId: card.dataset.messageTask});
    };
    history.addEventListener("click", navigate);
    history.addEventListener("keydown", navigate);
  }
  const messages = $("#workflow-trace-messages");
  if (messages && messages.dataset.diagnosticsBound !== "1") {
    messages.dataset.diagnosticsBound = "1";
    messages.addEventListener("click", openDelegated);
    messages.addEventListener("keydown", openDelegated);
    messages.addEventListener("click", (event) => {
      const dependency = event.target.closest("[data-dependency-agent]");
      if (dependency) openWorkflowAgentTrace(dependency.dataset.dependencyAgent, {segment: dependency.dataset.dependencySegment});
      const related = event.target.closest("[data-related-tool]");
      if (related) {
        const call = document.querySelector(`[data-tool-call-id="${CSS.escape(related.dataset.relatedTool)}"]`);
        if (call) { (call.matches("details") ? call : call.querySelector("details"))?.setAttribute("open", ""); call.scrollIntoView({block: "center", behavior: "smooth"}); }
      }
      const button = event.target.closest("[data-tool-output-toggle], [data-tool-output-more]");
      if (!button) return;
      event.preventDefault();
      const panel = button.closest("[data-tool-output]"), state = workflowToolOutputState(panel.dataset.toolOutput);
      if (button.hasAttribute("data-tool-output-toggle")) state.open = !state.open;
      panel.open = state.open;
      if (state.open) loadWorkflowToolOutput(panel.dataset.toolOutput, {force: true});
    });
  }
  const tabs = $("#workflow-conversation-tabs");
  if (tabs && tabs.dataset.bound !== "1") {
    tabs.dataset.bound = "1";
    tabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-workflow-conversation-mode]");
      if (!button || button.disabled) return;
      setWorkflowConversationMode(button.dataset.workflowConversationMode);
    });
  }
  const back = $("#workflow-trace-back");
  if (back && back.dataset.bound !== "1") {
    back.dataset.bound = "1";
    back.addEventListener("click", () => setWorkflowConversationMode("history"));
  }
  const agent = $("#workflow-trace-agent");
  if (agent && agent.dataset.bound !== "1") {
    agent.dataset.bound = "1";
    agent.addEventListener("change", () => openWorkflowAgentTrace(agent.value));
  }
  const segment = $("#workflow-trace-segment");
  if (segment && segment.dataset.bound !== "1") {
    segment.dataset.bound = "1";
    segment.addEventListener("change", () => {
      ed.workflowTraceSegment = segment.value;
      ed.workflowTraceSegmentPinned = !!segment.value;
      ed.workflowTracePayload = null;
      loadWorkflowAgentTrace({ force: true });
    });
  }
  const sources = $("#workflow-trace-sources");
  if (sources && sources.dataset.bound !== "1") {
    sources.dataset.bound = "1";
    sources.addEventListener("click", (event) => {
      const button = event.target.closest("[data-trace-source]");
      if (!button || button.disabled || button.dataset.traceSource === ed.workflowTraceSource) return;
      ed.workflowTraceSource = button.dataset.traceSource;
      ed.workflowTracePayload = null;
      const search = $("#workflow-trace-search");
      if (search) search.value = "";
      loadWorkflowAgentTrace({ force: true });
    });
  }
  const search = $("#workflow-trace-search");
  if (search && search.dataset.bound !== "1") {
    search.dataset.bound = "1";
    search.addEventListener("input", () => {
      if (search.workflowTraceFrame) cancelAnimationFrame(search.workflowTraceFrame);
      search.workflowTraceFrame = requestAnimationFrame(renderWorkflowTraceContent);
    });
    search.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const list = $("#workflow-trace-messages");
      const count = Number(list?.dataset.searchCount || 0);
      if (!count) return;
      event.preventDefault();
      const current = Number(list?.dataset.searchIndex || 0);
      focusWorkflowTraceSearchMatch(current + (event.shiftKey ? -1 : 1));
    });
  }
  const copy = $("#workflow-trace-copy");
  if (copy && copy.dataset.bound !== "1") {
    copy.dataset.bound = "1";
    copy.addEventListener("click", async () => {
      const readable = (ed.workflowTracePayload?.source || ed.workflowTraceSource) === "messages";
      const content = readable
        ? workflowTraceReadableText(workflowTraceMessageRecords().records)
        : String(ed.workflowTracePayload?.content || "");
      if (!content) return;
      try {
        await navigator.clipboard.writeText(content);
        const old = copy.textContent;
        copy.textContent = "已复制";
        setTimeout(() => { copy.textContent = old; }, 1200);
      } catch {
        showStudioNotice("无法复制 Trace，请手动选择文本。", { type: "error", duration: 3200 });
      }
    });
  }
  const expand = $("#workflow-trace-expand");
  if (expand && expand.dataset.bound !== "1") {
    expand.dataset.bound = "1";
    expand.addEventListener("click", () => {
      ed.workflowTraceExpanded = !ed.workflowTraceExpanded;
      expand.classList.toggle("on", ed.workflowTraceExpanded);
      expand.title = ed.workflowTraceExpanded ? "收起 Trace 面板" : "展开 Trace 面板";
      syncWorkflowConversationMode();
      syncWorkflowPanelLayout({ remeasure: true });
    });
  }
}

function setScrollBottomImmediately(scroller) {
  if (!scroller) return;
  const inlineBehavior = scroller.style.scrollBehavior;
  scroller.style.scrollBehavior = "auto";
  scroller.scrollTop = scroller.scrollHeight;
  requestAnimationFrame(() => {
    if (scroller.style.scrollBehavior === "auto") scroller.style.scrollBehavior = inlineBehavior;
  });
}

function renderWorkflowConversation() {
  const list = $("#workflow-message-list");
  if (!list) return;
  bindWorkflowTraceControls();
  syncWorkflowConversationMode();
  if (ed.workflowConversationMode === "trace") {
    renderWorkflowAgentTrace();
    loadWorkflowAgentTrace();
    return;
  }
  const kind = editorPresentationKind();
  const running = kind === "dynamic" ? ed.status === "running" : isStaticActiveStatus(ed.status);
  const signature = outlineConversationStructureKey(kind);
  if (list.dataset.renderKey !== signature || !$("#workflow-agent-progress-console")) {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
    list.dataset.renderKey = signature;
    list.innerHTML = `<div class="outline-chat-lane workflow-conversation-lane" id="workflow-conversation-lane">
      ${outlineConversationLaneMarkup(kind, running, {
        idPrefix: "workflow",
        progressId: "workflow-agent-progress-console",
      })}
    </div>`;
    if (nearBottom) requestAnimationFrame(() => setScrollBottomImmediately(list));
  }
  renderOrchestrationProgress($("#workflow-agent-progress-console"), ed.feed || {}, {
    running,
    specialistArtifacts: ed.specialistArtifacts || {},
    scrollContainer: list,
    artifactMode: "links",
  });
  syncTaskConfig();
  syncWorkflowArtifactSelection();
  if (kind === "static") syncStaticHistoricalTurnFeeds();
  syncWorkflowComposer();
}

function syncWorkflowComposer() {
  const form = $("#workflow-chat-form");
  const input = $("#workflow-chat-input");
  const button = $("#workflow-chat-send");
  const helper = $("#workflow-chat-helper");
  if (!form || !input || !button || !helper) return;
  const copy = taskComposerCopy(ed.kind, ed.status);
  input.placeholder = copy.placeholder;
  input.disabled = copy.disabled || form.classList.contains("is-sending");
  button.disabled = input.disabled || !input.value.trim();
  helper.textContent = form.classList.contains("is-sending") ? "正在发送修改要求…" : copy.helper;
  form.classList.toggle("is-locked", copy.disabled);
}

function bindWorkflowComposer() {
  const form = $("#workflow-chat-form");
  const input = $("#workflow-chat-input");
  if (!form || !input || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(118, input.scrollHeight)}px`;
    syncWorkflowComposer();
  });
  input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      if (!$("#workflow-chat-send")?.disabled) form.requestSubmit();
    }
  });
  form.addEventListener("submit", sendWorkflowFollowup);
  syncWorkflowComposer();
}

async function sendWorkflowFollowup(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const input = $("#workflow-chat-input");
  const message = input?.value.trim();
  if (!message || taskComposerCopy(ed.kind, ed.status).disabled || !requireAuthentication()) return;
  form.classList.add("is-sending");
  syncWorkflowComposer();
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/continue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || "修改要求发送失败");
    input.value = "";
    input.style.height = "auto";
    const revisionId = Number(payload.deck_id);
    await loadDecks(revisionId);
    await openDeck(revisionId);
    setWorkspaceView("workflow");
  } catch (error) {
    showStudioNotice(`发送失败：${error.message}`, { type: "error", duration: 7000 });
  } finally {
    form.classList.remove("is-sending");
    syncWorkflowComposer();
  }
}

function renderWorkflowSidePanels() {
  renderWorkflowArtifacts();
  renderWorkflowConversation();
  bindWorkflowComposer();
  bindWorkflowCenterControls();
  syncWorkflowPanelLayout();
  syncWorkflowCenterPane();
}

function workflowDocumentSpecs() {
  const files = (ed.workflow?.artifacts?.groups || [])
    .filter((group) => ["knowledge", "plan"].includes(group.id))
    .flatMap((group) => group.files || []);
  return [["knowledge", "knowledge.md"],
    ["plan", "deck.md"], ["design", "design-brief.md"]]
    .map(([key, name]) => [key, files.find((file) => file.rel.split("/").pop() === name)])
    .filter(([, file]) => file);
}

function workflowDocumentArtifactSignature() {
  return JSON.stringify(workflowDocumentSpecs().map(([key, file]) =>
    [key, file.rel, file.updated_at, file.size]));
}

async function loadWorkflowDocuments(signature) {
  if (!ed.id || ed.workflowDocumentLoading === signature) return;
  const deckId = ed.id;
  ed.workflowDocumentLoading = signature;
  const specs = workflowDocumentSpecs();
  const entries = await Promise.all(specs.map(async ([key, file]) => {
    try {
      const response = await fetch(`${workflowArtifactUrl(file)}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) return [key, ""];
      return [key, await response.text()];
    } catch {
      return [key, ""];
    }
  }));
  if (ed.id !== deckId) return;
  ed.workflowDocuments = Object.fromEntries(entries);
  ed.workflowDocumentSignature = signature;
  ed.workflowDocumentLoading = "";
  renderWorkflowTaskPane();
}

function renderWorkflowTaskPane() {
  const host = $("#workflow-task-document");
  if (!host) return;
  const artifactSignature = workflowDocumentArtifactSignature();
  if (ed.workflowDocumentSignature !== artifactSignature) {
    if (host.dataset.loadingSignature !== artifactSignature) {
      host.dataset.loadingSignature = artifactSignature;
      host.innerHTML = '<div class="workflow-panel-empty">正在读取任务文本与已落盘规划…</div>';
    }
    loadWorkflowDocuments(artifactSignature);
    return;
  }
  const query = String(ed.fullQuery || outlineBriefData().query || "").trim();
  const documents = ed.workflowDocuments || {};
  const documentFiles = Object.fromEntries(workflowDocumentSpecs());
  const sections = [
    ["knowledge", "研究与材料事实依据", "KNOWLEDGE"],
    ["plan", "整册内容规划", "DECK PLAN"],
    ["design", "设计语言与视觉约定", "DESIGN BRIEF"],
  ].filter(([key]) => String(documents[key] || "").trim());
  const signature = JSON.stringify([query, sections.map(([key]) => [key, documents[key]])]);
  if (host.dataset.renderKey === signature) return;
  host.dataset.renderKey = signature;
  host.innerHTML = `<article class="workflow-task-query">
      <span>ORIGINAL BRIEF</span><h3>完整任务要求</h3><div>${escapeHtml(query || "正在读取任务要求…").replace(/\n/g, "<br>")}</div>
    </article>
    ${sections.map(([key, title, kicker]) => `<article class="workflow-task-section">
      <header><span>${kicker}</span><h3>${title}</h3><a href="${workflowArtifactUrl(documentFiles[key])}" target="_blank" rel="noopener">打开原文件 ↗</a></header>
      <div class="workflow-task-markdown">${agentMarkdownHtml(documents[key])}</div>
    </article>`).join("")}`;
}

function workflowReadyPages() {
  return [...ed.rendered].map(Number).filter((page) => page > 0).sort((a, b) => a - b);
}

function renderWorkflowPagePane() {
  const stage = $("#workflow-page-stage");
  const strip = $("#workflow-page-strip");
  const position = $("#workflow-page-position");
  const title = $("#workflow-page-title");
  const previous = $("#workflow-page-prev");
  const next = $("#workflow-page-next");
  if (!stage || !strip) return;
  const ready = workflowReadyPages();
  let page = Number(ed.workflowPage) || ready[0] || 1;
  if (ready.length && !ready.includes(page)) page = ready[0];
  ed.workflowPage = page;
  const making = muralMakingView();
  const preview = ready.includes(page);
  const signature = preview ? fileUrl(page) : JSON.stringify(making);
  if (stage.dataset.renderKey !== signature) {
    stage.dataset.renderKey = signature;
    stage.innerHTML = preview
      ? `<img src="${fileUrl(page)}" alt="第 ${page} 页渲染预览">`
      : muralMakingMarkup(making, true, {checklist: false});
  }
  const stripSignature = JSON.stringify([page, ready, ed.total, ed.staticStamp, ed.rtimes]);
  if (strip.dataset.renderKey !== stripSignature) {
    strip.dataset.renderKey = stripSignature;
    strip.innerHTML = Array.from({ length: Math.max(ed.total || 0, ready.at(-1) || 0) }, (_, index) => index + 1).map((number) => {
      const rendered = ready.includes(number);
      return `<button type="button" data-workflow-page="${number}" class="${number === page ? "on" : ""}${rendered ? " ready" : ""}"${rendered ? "" : " disabled"}>
        ${rendered ? `<img src="${fileUrl(number)}" alt="">` : ""}<span>${pad2(number)}</span>
      </button>`;
    }).join("");
  }
  const index = ready.indexOf(page);
  if (title) title.textContent = ready.includes(page) ? `第 ${pad2(page)} 页` : "页面预览";
  if (position) position.textContent = `${ready.includes(page) ? index + 1 : 0} / ${ready.length || "–"}`;
  if (previous) previous.disabled = index <= 0;
  if (next) next.disabled = index < 0 || index >= ready.length - 1;
}

function setWorkflowPage(page) {
  const value = Number(page);
  if (!workflowReadyPages().includes(value)) return;
  ed.workflowPage = value;
  renderWorkflowPagePane();
}

function syncWorkflowCenterPane() {
  const pane = ["graph", "task", "page", "artifact"].includes(ed.workflowPane)
    ? ed.workflowPane : "page";
  ed.workflowPane = pane;
  const switcher = $("#workflow-pane-switch");
  if (switcher) switcher.dataset.active = pane;
  switcher?.querySelectorAll("button").forEach((button) => {
    const active = button.dataset.workflowPane === pane;
    button.classList.toggle("on", active);
    button.setAttribute("aria-selected", String(active));
  });
  $$('[data-workflow-pane-panel]').forEach((panel) => { panel.hidden = panel.dataset.workflowPanePanel !== pane; });
  const hint = $("#workflow-pane-hint");
  if (hint) hint.textContent = ({ graph: "点击卡片选中 · 右上角「查看迭代」进入下钻", task: "完整需求、研究与规划文本", page: "同一工作台查看实际页面", artifact: "当前选中的任务产物" })[pane];
  if (pane === "task") renderWorkflowTaskPane();
  if (pane === "page") renderWorkflowPagePane();
  if (pane === "artifact") renderWorkflowArtifactPane();
  syncWorkflowLayoutReset();
}

function setWorkflowPane(pane) {
  const next = ["graph", "task", "page", "artifact"].includes(pane) ? pane : "page";
  if (ed.workflowPane === next) return;
  ed.workflowPane = next;
  syncWorkflowCenterPane();
  if (next === "graph") {
    const stage = $("#workflow-stage");
    if (stage) delete stage.dataset.workflowSignature;
    renderWorkflowCanvas();
  }
}

function workflowSplitRatio(value) {
  const ratio = Number(value);
  return Math.max(.25, Math.min(.65, value == null || value === "" || !Number.isFinite(ratio) ? .4 : ratio));
}

function setWorkflowSplit(value, { remember = true } = {}) {
  const ratio = workflowSplitRatio(value);
  const shell = $(".workflow-studio-shell"), splitter = $("#workflow-splitter");
  shell?.style.setProperty("--workflow-center-fr", `${1 - ratio}fr`);
  shell?.style.setProperty("--workflow-message-fr", `${ratio}fr`);
  splitter?.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
  splitter?.setAttribute("aria-valuetext", `右侧消息宽度偏好 ${Math.round(ratio * 100)}%，窄屏自动适配`);
  if (remember) {
    ed.workflowSplitRatio = ratio;
    ed.workflowTraceExpanded = false;
    shell?.classList.remove("trace-expanded");
    const expand = $("#workflow-trace-expand");
    expand?.classList.remove("on");
    if (expand) expand.title = "展开 Trace 面板";
    localStorage.setItem("studio_workflow_split_ratio", String(ratio));
  }
}

function bindWorkflowSplitter() {
  const splitter = $("#workflow-splitter"), shell = $(".workflow-studio-shell");
  if (!splitter || !shell || splitter.dataset.bound === "1") return;
  splitter.dataset.bound = "1";
  let drag = null;
  splitter.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    const center = $(".workflow-canvas-panel").getBoundingClientRect();
    const messages = $(".workflow-conversation").getBoundingClientRect();
    drag = { pointer: event.pointerId, start: event.clientX, width: messages.width,
      total: center.width + messages.width };
    splitter.setPointerCapture(event.pointerId);
    shell.classList.add("resizing");
    event.preventDefault();
  });
  splitter.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    setWorkflowSplit((drag.width + drag.start - event.clientX) / drag.total);
  });
  const finish = () => {
    if (!drag) return;
    const pointer = drag.pointer;
    drag = null;
    if (splitter.hasPointerCapture(pointer)) splitter.releasePointerCapture(pointer);
    shell.classList.remove("resizing");
    syncWorkflowPanelLayout({ remeasure: true });
  };
  ["pointerup", "pointercancel", "lostpointercapture"].forEach(type => splitter.addEventListener(type, finish));
  splitter.addEventListener("dblclick", () => { setWorkflowSplit(.4); syncWorkflowPanelLayout({ remeasure: true }); });
  splitter.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const value = Number(splitter.getAttribute("aria-valuenow")) / 100;
    setWorkflowSplit(event.key === "Home" ? .25 : event.key === "End" ? .65
      : value + (event.key === "ArrowLeft" ? .02 : -.02));
    syncWorkflowPanelLayout({ remeasure: true });
  });
}

function syncWorkflowPanelLayout({ remeasure = false } = {}) {
  const shell = $(".workflow-studio-shell");
  if (!shell) return;
  const state = ed.workflowPanelCollapsed ||= { files: false, conversation: false };
  shell.classList.toggle("files-collapsed", !!state.files);
  shell.classList.toggle("conversation-collapsed", !!state.conversation);
  setWorkflowSplit(ed.workflowConversationMode === "trace" && ed.workflowTraceExpanded ? .6 : ed.workflowSplitRatio, { remember: false });
  const controls = [
    [$("#workflow-files-toggle"), "files", "任务与产物"],
    [$("#workflow-conversation-toggle"), "conversation", "历史与消息"],
  ];
  controls.forEach(([button, key, label]) => {
    if (!button) return;
    const collapsed = !!state[key];
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", `${collapsed ? "展开" : "折叠"}${label}`);
    button.title = `${collapsed ? "展开" : "折叠"}${label}`;
  });
  if (!remeasure || ed.workspaceView !== "workflow") return;
  const refresh = () => {
    const stage = $("#workflow-stage");
    if (stage) delete stage.dataset.workflowSignature;
    renderWorkflowCanvas();
  };
  requestAnimationFrame(refresh);
  setTimeout(refresh, 280);
}

function bindWorkflowCenterControls() {
  bindWorkflowOwnerGraph();
  bindWorkflowSplitter();
  const switcher = $("#workflow-pane-switch");
  if (switcher && switcher.dataset.bound !== "1") {
    switcher.dataset.bound = "1";
    switcher.addEventListener("click", (event) => {
      const button = event.target.closest("[data-workflow-pane]");
      if (button) setWorkflowPane(button.dataset.workflowPane);
    });
  }
  const stage = $("#workflow-stage");
  if (stage && stage.dataset.pageBound !== "1") {
    stage.dataset.pageBound = "1";
    stage.addEventListener("click", (event) => {
      const button = event.target.closest("[data-workflow-page]");
      if (button) {
        ed.workflowPage = Number(button.dataset.workflowPage);
        setWorkflowPane("page");
        renderWorkflowPagePane();
        return;
      }
      if (stage._workflowSuppressNodeClick) return;
      const drilldown = event.target.closest("[data-workflow-drilldown]");
      if (drilldown) {
        const card = drilldown.closest(".workflow-node");
        ed.workflowSelectedNodeId = card?.dataset.nodeId || "";
        openWorkflowOwnerGraph(drilldown.dataset.workflowDrilldown,
          {segment: card?.dataset.workflowSegment || "", taskId: card?.dataset.workflowTaskId || ""});
        return;
      }
      const node = event.target.closest(".workflow-node[data-agent-key]");
      if (node) {
        ed.workflowSelectedNodeId = node.dataset.nodeId;
        if (node.dataset.agentKey) openWorkflowAgentTrace(node.dataset.agentKey, {segment: node.dataset.workflowSegment || ""});
        else setWorkflowConversationMode("history", {clearAgent: true});
        return;
      }
      if (!event.target.closest(".workflow-node") && (ed.workflowTraceAgent || ed.workflowSelectedNodeId)) {
        ed.workflowSelectedNodeId = "";
        setWorkflowConversationMode("history");
      }
    });
    stage.addEventListener("keydown", (event) => {
      if (event.target.closest("button, a, input, textarea, select")) return;
      const node = event.target.closest(".workflow-node[data-agent-key]");
      if (!node || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      node.click();
    });
  }
  bindWorkflowNodeDragging(stage);
  const layoutReset = $("#workflow-layout-reset");
  if (layoutReset && layoutReset.dataset.bound !== "1") {
    layoutReset.dataset.bound = "1";
    layoutReset.addEventListener("click", () => {
      ed.workflowNodeOffsets = {};
      delete stage?.dataset.workflowSignature;
      renderWorkflowCanvas();
      syncWorkflowLayoutReset();
    });
  }
  [
    [$("#workflow-files-toggle"), "files"],
    [$("#workflow-conversation-toggle"), "conversation"],
  ].forEach(([button, key]) => {
    if (!button || button.dataset.bound === "1") return;
    button.dataset.bound = "1";
    button.addEventListener("click", () => {
      const state = ed.workflowPanelCollapsed ||= { files: false, conversation: false };
      state[key] = !state[key];
      syncWorkflowPanelLayout({ remeasure: true });
    });
  });
  const strip = $("#workflow-page-strip");
  if (strip && strip.dataset.bound !== "1") {
    strip.dataset.bound = "1";
    strip.addEventListener("click", (event) => {
      const button = event.target.closest("[data-workflow-page]");
      if (button && !button.disabled) setWorkflowPage(button.dataset.workflowPage);
    });
  }
  const previous = $("#workflow-page-prev");
  if (previous && previous.dataset.bound !== "1") {
    previous.dataset.bound = "1";
    previous.addEventListener("click", () => {
      const ready = workflowReadyPages();
      setWorkflowPage(ready[Math.max(0, ready.indexOf(Number(ed.workflowPage)) - 1)]);
    });
  }
  const next = $("#workflow-page-next");
  if (next && next.dataset.bound !== "1") {
    next.dataset.bound = "1";
    next.addEventListener("click", () => {
      const ready = workflowReadyPages();
      setWorkflowPage(ready[Math.min(ready.length - 1, ready.indexOf(Number(ed.workflowPage)) + 1)]);
    });
  }
  const open = $("#workflow-page-open");
  if (open && open.dataset.bound !== "1") {
    open.dataset.bound = "1";
    open.addEventListener("click", () => {
      const page = Number(ed.workflowPage) || 1;
      setWorkspaceView("ppt");
      select(page, { byUser: true });
    });
  }
  const bindArtifactSelection = (root) => {
    if (!root || root.dataset.artifactBound === "1") return;
    root.dataset.artifactBound = "1";
    root.addEventListener("click", (event) => {
      const link = event.target.closest("[data-workflow-artifact-rel]");
      if (!link) return;
      event.preventDefault();
      selectWorkflowArtifact(link.dataset.workflowArtifactRel, {
        name: link.dataset.workflowArtifactName,
        kind: link.dataset.workflowArtifactKind,
      });
    });
  };
  bindArtifactSelection($("#workflow-file-tree"));
  bindArtifactSelection($("#workflow-message-list"));
  bindArtifactSelection($("#workflow-trace-messages"));
}

let workflowEdgeDrawFrame = 0;

function scheduleWorkflowEdgeDraw(stage) {
  if (!stage || workflowEdgeDrawFrame) return;
  workflowEdgeDrawFrame = requestAnimationFrame(() => {
    workflowEdgeDrawFrame = 0;
    if (stage.isConnected) workflowDrawEdges(stage, stage._workflowEdges || []);
  });
}

function syncWorkflowLayoutReset() {
  const reset = $("#workflow-layout-reset");
  if (!reset) return;
  const changed = Object.keys(ed.workflowNodeOffsets || {}).length > 0;
  reset.hidden = ed.workflowPane !== "graph" || !!(ed.workflowOwnerAgent || ed.workflowOwnerTask);
  reset.disabled = !changed;
  reset.classList.toggle("active", changed);
}

function bindWorkflowNodeDragging(stage) {
  if (!stage || stage.dataset.dragBound === "1") return;
  stage.dataset.dragBound = "1";
  let drag = null;
  stage.addEventListener("pointerdown", (event) => {
    const node = event.target.closest(".workflow-node");
    if (!node || event.button !== 0 || event.target.closest("button, a, input, textarea, select")) return;
    event.preventDefault();
    drag = {
      node,
      pointerId: event.pointerId,
      pointerX: event.clientX,
      pointerY: event.clientY,
      left: node.offsetLeft,
      top: node.offsetTop,
      baseX: Number(node.dataset.baseX) || 0,
      baseY: Number(node.dataset.baseY) || 0,
      moved: false,
    };
    node.classList.add("dragging");
    node.setPointerCapture?.(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (Math.hypot(event.clientX - drag.pointerX, event.clientY - drag.pointerY) > 4) drag.moved = true;
    const left = Math.max(12, Math.min(stage.clientWidth - drag.node.offsetWidth - 12, drag.left + event.clientX - drag.pointerX));
    const top = Math.max(48, Math.min(stage.clientHeight - drag.node.offsetHeight - 18, drag.top + event.clientY - drag.pointerY));
    drag.node.style.left = `${Math.round(left)}px`;
    drag.node.style.top = `${Math.round(top)}px`;
    ed.workflowNodeOffsets ||= {};
    ed.workflowNodeOffsets[drag.node.dataset.nodeId] = {
      x: Math.round(left - drag.baseX),
      y: Math.round(top - drag.baseY),
    };
    syncWorkflowLayoutReset();
    scheduleWorkflowEdgeDraw(stage);
  });
  const finish = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const moved = drag.moved;
    drag.node.classList.remove("dragging");
    drag.node.releasePointerCapture?.(event.pointerId);
    drag = null;
    if (moved) {
      stage._workflowSuppressNodeClick = true;
      requestAnimationFrame(() => { stage._workflowSuppressNodeClick = false; });
    }
    scheduleWorkflowEdgeDraw(stage);
  };
  stage.addEventListener("pointerup", finish);
  stage.addEventListener("pointercancel", finish);
}

function workflowDrawEdges(stage, edges) {
  const width = stage.offsetWidth;
  const height = stage.offsetHeight;
  let svg = stage.querySelector(".workflow-edge-layer");
  if (!svg) {
    svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "workflow-edge-layer");
    svg.innerHTML = `<defs>
    <marker id="workflow-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 8 4 0 8Z" fill="#8064df" opacity=".72"/></marker>
    <marker id="workflow-arrow-repair" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 8 4 0 8Z" fill="#ed745d" opacity=".86"/></marker>
  </defs>`;
    stage.prepend(svg);
  }
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const previous = new Map([...svg.querySelectorAll(".workflow-edge")].map(path => [path.dataset.edgeKey, path]));
  const nodes = new Map([...stage.querySelectorAll(".workflow-node")].map((node) => [node.dataset.nodeId, node]));
  const portClasses = ["uses-port-left", "uses-port-right", "uses-port-top", "uses-port-bottom"];
  nodes.forEach((node) => node.classList.remove(...portClasses));
  const point = (node, port) => {
    const left = node.offsetLeft;
    const top = node.offsetTop;
    const width = node.offsetWidth;
    const height = node.offsetHeight;
    if (port === "left") return { x: left, y: top + height / 2 };
    if (port === "right") return { x: left + width, y: top + height / 2 };
    if (port === "top") return { x: left + width / 2, y: top };
    return { x: left + width / 2, y: top + height };
  };
  const portsFor = (from, to, edge) => {
    if (edge.fromPort && edge.toPort) return [edge.fromPort, edge.toPort];
    const ax = from.offsetLeft + from.offsetWidth / 2;
    const ay = from.offsetTop + from.offsetHeight / 2;
    const bx = to.offsetLeft + to.offsetWidth / 2;
    const by = to.offsetTop + to.offsetHeight / 2;
    if (edge.axis === "vertical" || Math.abs(by - ay) >= Math.abs(bx - ax) * .62) {
      return by >= ay ? ["bottom", "top"] : ["top", "bottom"];
    }
    return bx >= ax ? ["right", "left"] : ["left", "right"];
  };
  edges.forEach((edge, index) => {
    const from = nodes.get(edge.from), to = nodes.get(edge.to);
    if (!from || !to) return;
    const [fromPort, toPort] = portsFor(from, to, edge);
    from.classList.add(`uses-port-${fromPort}`);
    to.classList.add(`uses-port-${toPort}`);
    const source = point(from, fromPort);
    const target = point(to, toPort);
    const sx = source.x, sy = source.y, tx = target.x, ty = target.y;
    let path;
    if (edge.kind === "repair") {
      const lane = fromPort === "right"
        ? Math.min(width - 14, Math.max(sx, tx) + 54 + index * 6)
        : Math.max(14, Math.min(sx, tx) - 54 - index * 6);
      path = `M${sx},${sy} C${lane},${sy} ${lane},${ty} ${tx},${ty}`;
    } else if (fromPort === "top" || fromPort === "bottom") {
      const direction = fromPort === "bottom" ? 1 : -1;
      const bend = Math.max(36, Math.abs(ty - sy) * .46);
      path = `M${sx},${sy} C${sx},${sy + bend * direction} ${tx},${ty - bend * direction} ${tx},${ty}`;
    } else {
      const direction = fromPort === "right" ? 1 : -1;
      const bend = Math.max(42, Math.abs(tx - sx) * .46);
      path = `M${sx},${sy} C${sx + bend * direction},${sy} ${tx - bend * direction},${ty} ${tx},${ty}`;
    }
    const key = JSON.stringify([edge.from, edge.to, edge.kind || "", fromPort, toPort]);
    let base = previous.get(key), flow = base?.nextElementSibling;
    previous.delete(key);
    if (!base) {
      base = document.createElementNS("http://www.w3.org/2000/svg", "path");
      base.setAttribute("class", "workflow-edge");
      base.dataset.edgeKey = key;
      base.dataset.from = edge.from;
      base.dataset.to = edge.to;
      const relation = document.createElementNS("http://www.w3.org/2000/svg", "title");
      relation.textContent = "流程示意：职责顺序，不表示实时依赖或消息传输";
      base.appendChild(relation);
      flow = document.createElementNS("http://www.w3.org/2000/svg", "path");
      flow.style.animationDelay = `${(index % 7) * -170}ms`;
      svg.append(base, flow);
    }
    base.setAttribute("d", path);
    const active = to.classList.contains("running");
    const targetComplete = to.classList.contains("complete");
    flow.setAttribute("class", `workflow-edge-flow${edge.kind === "repair" ? " repair" : targetComplete ? " complete" : ""}${active ? " active" : ""}`);
    flow.setAttribute("d", path);
    flow.setAttribute("marker-end", edge.kind === "repair" ? "url(#workflow-arrow-repair)" : "url(#workflow-arrow)");
  });
  previous.forEach(base => { base.nextElementSibling.remove(); base.remove(); });
}

function workflowExecutionStatus(status, events = []) {
  const value = ({succeeded: "complete", completed: "complete", pending: "not_started",
    waiting_dependency: "waiting_dependencies", blocked: "waiting"})[status] || status || "unknown";
  if (value !== "running") return value;
  const last = [...events].reverse().find(event => ["resource_wait_started", "resource_wait_completed",
    "resource_wait_failed", "provider_started", "agent_started"].includes(event.event_type));
  return last?.event_type === "resource_wait_started" ? "resource_wait"
    : last?.event_type === "resource_wait_failed" ? "failed" : value;
}

function workflowCurrentActivity(events) {
  const latest = [...events].reverse().find(event => runtimeEventState(event)
    || ["tool", "tool_result", "text", "analysis"].includes(event.k));
  if (!latest) return null;
  const runtime = runtimeEventState(latest);
  if (runtime) return {title: runtime[0], detail: runtime[1]};
  if (latest.k === "tool" || latest.k === "tool_result") return {
    title: `${latest.k === "tool" ? "执行工具" : latest.ok === false ? "工具失败" : "工具已返回"} · ${latest.tool || "未记录名称"}`,
    detail: String(latest.hint || latest.error_code || ""),
  };
  return {title: latest.k === "analysis" ? "可展示分析摘要" : "Owner 消息", detail: String(latest.s || "")};
}

function workflowTaskGraph(tasks, timings = {}, feed = {}, deckStatus = "") {
  const byId = new Map(tasks.filter(task => task.task_id).map(task => [task.task_id, task]));
  const roleNames = {research: "资料研究", image: "图片素材", slide: "页面制作"};
  const pageLabel = task => {
    const pages = [...new Set(task.pages || [])].sort((a, b) => a - b);
    return pages.length ? `P${pages.join(" / P")}` : "";
  };
  const title = task => `${pageLabel(task)} ${roleNames[task.role] || task.role || "任务"}`.trim();
  const rawNodes = [...byId.values()].map((task, taskIndex) => {
    const key = task.agent_key
      || Object.entries(timings).find(([, timing]) => timing.task_id === task.task_id)?.[0]
      || Object.entries(feed).find(([, events]) => events.some(event => event.task_id === task.task_id
        && event.role === task.role && ["agent_started", "agent_completed", "agent_failed"].includes(event.event_type)))?.[0]
      || "";
    const timing = timings[key] || {};
    const current = timing.task_id === task.task_id;
    const started = [...(feed[key] || [])].reverse().find(event =>
      event.event_type === "agent_started" && event.task_id === task.task_id);
    const segment = started?.segment_id || task.result?.segment_ids?.at(-1) || "";
    const dependencies = task.dependencies || [];
    const failed = dependencies.filter(id => ["failed", "cancelled", "skipped"].includes(byId.get(id)?.status));
    let status = workflowExecutionStatus(task.status);
    if (current && timing.execution_state) status = timing.execution_state;
    if (["failed", "skipped"].includes(task.status) && failed.length && !segment) status = "dependency_failed";
    const pending = dependencies.filter(id => !["succeeded", "completed"].includes(byId.get(id)?.status));
    if (["not_started", "queued"].includes(status) && pending.length) status = "waiting_dependencies";
    const events = (feed[key] || []).filter(event => event.task_id
      ? event.task_id === task.task_id : segment && event.segment_id === segment);
    status = workflowExecutionStatus(status, events);
    const ended = ["completed", "failed", "rejected", "interrupted", "paused_recoverable", "cancelled"].includes(deckStatus);
    const unclosed = ended && ["running", "resource_wait", "queued", "waiting_dependencies", "not_started"].includes(status);
    if (unclosed) status = "unknown";
    const detail = unclosed ? "本次运行已结束，任务记录尚未闭合；请查看 Trace。"
      : failed.length && status === "dependency_failed" ? `上游 ${failed.map(id => `${title(byId.get(id))} (${id})`).join("、")} 失败或取消；本任务未启动。`
      : status === "resource_wait" ? "正在等待本地模型资源，尚未发送本轮请求。"
      : status === "waiting_dependencies" ? `等待：${pending.map(id => byId.has(id) ? title(byId.get(id)) : id).join("、")}`
      : current && timing.status_detail ? timing.status_detail
      : String(task.error || (dependencies.length ? "前置依赖已满足。" : "无前置依赖，可独立调度。"));
    const siblings = task.owner_id ? [...byId.values()].filter(row => row.owner_id === task.owner_id) : [task];
    const turn = Math.max(0, ...events.map(event => Number(event.turn) || 0));
    const receipts = new Set(events.filter(event => event.k === "tool_result" && event.tool_use_id).map(event => event.tool_use_id)).size;
    return {id: `task:${task.task_id}`, taskId: task.task_id, taskIds: [task.task_id],
      taskIndex, ownerId: task.owner_id || "", ownerAction: task.owner_action || "", role: task.role,
      agentKey: key, segment, status,
      activity: workflowCurrentActivity(events),
      progress: [turn ? `第 ${turn} 回合` : "", receipts ? `${receipts} 次工具回执` : ""].filter(Boolean).join(" · "),
      title: title(task), summary: String(task.goal || ""), identity: [task.task_id, task.owner_id].filter(Boolean).join(" · "),
      code: siblings.length > 1 ? `同一 Owner · 批次 ${siblings.indexOf(task) + 1}/${siblings.length}` : `尝试 ${task.attempt || 1}`,
      kicker: `${task.role || "Task"}${task.repair ? " · 修订" : ""}`,
      detail, pages: task.pages || []};
  });
  const grouped = new Map();
  rawNodes.forEach(node => {
    const key = node.role === "slide" && node.ownerId ? `slide-owner:${node.ownerId}` : node.id;
    const rows = grouped.get(key) || [];
    rows.push(node);
    grouped.set(key, rows);
  });
  const urgency = ["running", "resource_wait", "dependency_failed", "failed", "start_failed",
    "waiting_dependencies", "queued", "interrupted", "cancelled", "skipped", "observed",
    "not_started", "waiting"];
  const taskToNodeId = new Map();
  const nodes = [...grouped.entries()].map(([groupKey, rows]) => {
    rows.sort((a, b) => a.taskIndex - b.taskIndex);
    if (rows.length === 1) {
      taskToNodeId.set(rows[0].taskId, rows[0].id);
      return rows[0];
    }
    const status = urgency.find(key => rows.some(row => row.status === key))
      || (rows.every(row => row.status === "complete") ? "complete" : rows.at(-1).status || "unknown");
    const current = rows.find(row => timings[row.agentKey]?.task_id === row.taskId);
    const active = rows.find(row => row.status === status);
    const latest = current || active || rows.at(-1);
    const pages = [...new Set(rows.flatMap(row => row.pages || []))].sort((a, b) => a - b);
    const taskIds = rows.flatMap(row => row.taskIds || [row.taskId]).filter(Boolean);
    const repairs = rows.filter(row => String(row.ownerAction || "").toLowerCase() === "reuse_owner"
      || /repair/i.test(String(row.taskId || ""))).length || Math.max(0, rows.length - 1);
    const node = {
      ...latest,
      id: groupKey,
      taskIds,
      pages,
      repairs,
      status,
      title: `${pages.length ? `P${pages.join(" / P")}` : ""} ${roleNames.slide}`.trim(),
      code: `同一 Owner · ${rows.length} 批次`,
      kicker: repairs ? "slide · 修订回流" : "slide",
      identity: [latest.taskId, latest.ownerId].filter(Boolean).join(" · "),
      detail: [`已合并显示 ${rows.length} 个同一 Owner 执行段，当前批次：${latest.taskId}。`, latest.detail]
        .filter(Boolean).join(" "),
      summary: rows.map(row => row.summary).find(Boolean) || latest.summary,
    };
    taskIds.forEach(id => taskToNodeId.set(id, node.id));
    return node;
  });
  const edges = [...byId.values()].flatMap(task => (task.dependencies || [])
    .filter(id => byId.has(id)).map(id => ({from: taskToNodeId.get(id) || `task:${id}`,
      to: taskToNodeId.get(task.task_id) || `task:${task.task_id}`})))
    .filter(edge => edge.from !== edge.to);
  const seenEdges = new Set();
  return {nodes, edges: edges.filter(edge => {
    const key = `${edge.from}\u0000${edge.to}`;
    if (seenEdges.has(key)) return false;
    seenEdges.add(key);
    return true;
  })};
}

function workflowOverview(graph, state = ed) {
  const events = state.feed?.orch || [];
  const timing = state.agentTimings?.orch || {};
  const selfAuthored = Object.entries(state.pageAgents || {}).filter(([, owner]) => owner === "orch")
    .map(([page]) => Number(page)).filter(page => page > 0).sort((a, b) => a - b);
  const lifecycle = [...events].reverse().find(event => ["agent_started", "agent_completed", "agent_failed"].includes(event.event_type));
  let status = workflowExecutionStatus(lifecycle?.status || ({agent_started: "running", agent_completed: "complete", agent_failed: "failed"})[lifecycle?.event_type]
    || timing.execution_state || timing.status, events);
  const ended = ["completed", "failed", "rejected", "interrupted", "paused_recoverable", "cancelled"].includes(state.status);
  if (ended && ["running", "resource_wait"].includes(status)) status = "interrupted";
  const coordinator = {id: "orch", agentKey: "orch", role: "orchestrator", status,
    title: selfAuthored.length ? "主 Agent 制作 · 整册统筹" : "Orchestrator · 整册统筹",
    kicker: "主 Agent", code: "ORCH", pages: selfAuthored,
    activity: workflowCurrentActivity(events),
    progress: [selfAuthored.length ? `自制 ${selfAuthored.length} 页` : "",
      graph.nodes.length ? `委派任务完成 ${graph.nodes.filter(node => node.status === "complete").length} / ${graph.nodes.length}`
        : selfAuthored.length ? "无子任务" : "尚无委派任务"].filter(Boolean).join(" · "),
    detail: status === "unknown" ? "尚无启动记录，不代表模型正在执行。"
      : status === "interrupted" ? "本次运行已结束，尚无 Orchestrator 完成记录。"
      : timing.status_detail || "理解任务、组织委派与整册验收"};
  const summarize = (id, label, nodes) => {
    const counts = new Map();
    nodes.forEach(node => counts.set(node.status, (counts.get(node.status) || 0) + 1));
    return {id, label, status: ["running", "resource_wait", "dependency_failed", "failed", "start_failed", "waiting_dependencies", "queued", "interrupted", "cancelled", "skipped", "observed", "not_started", "waiting"]
      .find(key => counts.has(key)) || (nodes.length && nodes.every(node => node.status === "complete") ? "complete" : "unknown"),
    detail: nodes.length ? [...counts].map(([key, count]) => `${count} 项${workflowStatusLabel(key)}`).join(" · ") : "尚无委派记录"};
  };
  const phases = [summarize("research", "研究与规划", graph.nodes.filter(node => node.role === "research")),
    summarize("image", "素材准备", graph.nodes.filter(node => node.role === "image")),
    summarize("slide", "页面制作", graph.nodes.filter(node => node.role === "slide"))];
  if (selfAuthored.length) {
    const pagePhase = phases[2];
    const delivered = state.status === "completed" && state.artifactStatus === "complete";
    if (["unknown", "complete"].includes(pagePhase.status)) pagePhase.status = delivered ? "complete" : "observed";
    pagePhase.detail = [graph.nodes.some(node => node.role === "slide") ? pagePhase.detail : "",
      `主 Agent 制作：P${selfAuthored.join("、P")}；${delivered ? "交付校验已通过" : "已记录页面责任，不代表检查完成"}`].filter(Boolean).join("；");
  }
  const files = (state.workflow?.artifacts?.groups || []).flatMap(group => group.files || []);
  if (files.some(file => file.rel === "deck.md")) {
    phases[0].detail += "；已有分页规划文件";
    if (phases[0].status === "unknown") phases[0].status = "observed";
  }
  if ((state.attachments || []).length || (state.attachmentParse || []).length) {
    phases.unshift(summarize("attachments", "附件解析", (state.attachmentParse || []).map(row => ({
      status: ({processing: "running", completed: "complete", partial: "observed"})[row.status] || row.status}))));
    phases[0].detail += "；不代表 Research 已阅读";
  }
  const transactions = liveToolTransactions(events);
  const verify = transactions.find(row => row.tool === "verification_submit");
  const latestReview = [...events].reverse().find(event => event.event_type === "deck_completion_checked"
    || event.tool === "verification_submit");
  const completion = latestReview?.event_type === "deck_completion_checked" ? latestReview : null;
  const visual = liveToolTransactions(events.filter(event => ["inspect", "vision_analyze"].includes(event.tool)))
    .find(row => row.tool === "inspect" ? row.use?.args?.page !== undefined : ["deck", "page"].includes(row.use?.args?.scope));
  const reviewing = !completion && verify?.use && !verify.result;
  const ready = completion ? completion.status === "ready"
    : verify?.result?.ok === true && verify.use?.args?.status === "ready";
  const openTasks = graph.nodes.some(node => node.status !== "complete");
  const completionPending = completion && !(state.status === "completed" && state.artifactStatus === "complete");
  phases.push({id: "review", label: "整册检查", status: reviewing ? ended ? "interrupted" : "running"
    : ready ? openTasks || completionPending ? "observed" : "complete" : completion || verify ? "waiting"
      : visual ? visual.result ? "observed" : ended ? "interrupted" : "running" : "unknown",
    detail: reviewing ? ended ? "运行已结束，验收工具未返回" : "正在执行整册验收工具"
      : ready ? openTasks ? "已有通过记录；仍有未闭合任务"
        : completion ? `Harness 自动制作完成校验已通过；${completionPending ? "当前制作尚未收尾" : "不代表离线内容 QC 或 clean 准入"}`
        : "最近整册验收工具已通过"
      : completion ? "Harness 自动完成校验未通过；具体缺口见后台回执"
      : verify ? "最近验收未通过或仍需修订"
      : visual ? `Orchestrator ${visual.tool === "inspect" && Array.isArray(visual.use.args.page) && visual.use.args.page.length > 1 ? "多页联系表" : visual.use.args.scope === "deck" ? "整册" : "页面"}视觉复核：${visual.result ? visual.result.ok ? "图片已返回，判断见自身消息" : "看图工具失败" : "等待看图回执"}；尚无整册验收回执`
      : "尚无整册验收回执"});
  const delivered = state.status === "completed" && state.artifactStatus === "complete";
  phases.push({id: "delivery", label: "交付产物", status: delivered ? "complete" : "unknown",
    detail: delivered ? "交付校验已通过；导出格式按需生成" : "尚无有效交付完成回执"});
  const submitted = new Set((state.muralRuntime?.deck_timeline || [])
    .filter(row => row.type === "task_submitted").map(row => row.task_id));
  transactions.filter(row => row.tool === "delegate_submit" && row.result?.ok === true)
    .forEach(row => (row.use?.args?.tasks || []).forEach(task => submitted.add(task.task_id)));
  return {coordinator, phases, delegated: graph.nodes
    .filter(node => [...new Set([node.taskId, ...(node.taskIds || [])])].some(id => submitted.has(id)))
    .map(node => node.id),
    query: state.fullQuery || state.briefMeta?.user_query || "尚未收到用户任务",
    active: phases.filter(phase => phase.status === "running").map(phase => phase.label),
    blocked: graph.nodes.filter(node => ["dependency_failed", "waiting_dependencies", "resource_wait", "failed", "start_failed"].includes(node.status))};
}

function renderWorkflowOverview(view) {
  const host = $("#workflow-overview");
  if (!host) return;
  const signature = JSON.stringify([view.query, view.active, view.phases, view.blocked.map(node => [node.title, node.status])]);
  if (host.dataset.signature === signature) return;
  host.dataset.signature = signature;
  const expanded = host.querySelector("details[open]") != null;
  host.innerHTML = `<details class="workflow-query"${expanded ? " open" : ""}><summary><span>用户任务</span><b>${escapeHtml(view.query)}</b></summary><p>${escapeHtml(view.query)}</p></details>
    <div class="workflow-overview-heading"><strong>${view.active.length ? `正在推进：${escapeHtml(view.active.join("、"))}` : "整册进展"}</strong><span>Orchestrator 统筹 · 各项可并行</span></div>
    <div class="workflow-phases">${view.phases.map(phase => `<div class="workflow-phase ${phase.status}" data-workflow-phase="${phase.id}"><b><i></i>${phase.label}</b><span>${escapeHtml(phase.detail)}</span></div>`).join("")}</div>
    ${view.blocked.length ? `<p class="workflow-blockers">需关注：${view.blocked.slice(0, 3).map(node => `${escapeHtml(node.title)} · ${workflowStatusLabel(node.status)}`).join("；")}${view.blocked.length > 3 ? `；另 ${view.blocked.length - 3} 项` : ""}</p>` : ""}
    <div class="workflow-relations"><span><i class="dependency"></i>分层流程示意，不表示实际等待依赖</span><span><i class="active"></i>高亮表示目标正在执行</span><span>点击卡片选中 · 右上角「查看迭代」下钻</span></div>`;
}

function workflowFlowLayout(tasks, view, viewportWidth, state = ed) {
  const files = (state.workflow?.artifacts?.groups || []).flatMap(group => group.files || []);
  const planned = files.some(file => file.rel === "deck.md");
  const briefs = files.filter(file => /^briefs\/slide_\d+\.md$/.test(file.rel)).length;
  const rendered = state.rendered?.size || 0;
  const phase = id => view.phases.find(item => item.id === id);
  const groups = [
    {label: "用户任务", nodes: [{id: "query", role: "query", status: state.fullQuery ? "observed" : "unknown",
      title: "用户任务", kicker: "Input", code: "BRIEF", detail: view.query}]},
    {label: "整册统筹", nodes: [view.coordinator]},
    {label: "研究与知识", nodes: tasks.filter(node => !["image", "slide"].includes(node.role))},
    {label: "共享规划", nodes: [{id: "blueprint", role: "blueprint", status: planned ? "observed" : "unknown",
      title: "整册与分页规划", kicker: "Shared plan", code: "PLAN",
      detail: planned ? `deck.md 已落盘 · ${briefs} 份分页计划；不代表内容验收。` : "尚未记录整册规划文件。"}]},
    {label: "图片与素材", nodes: tasks.filter(node => node.role === "image")},
    {label: "页面制作 · Slide Owners", nodes: tasks.filter(node => node.role === "slide")
      .sort((a, b) => Math.min(...a.pages, Infinity) - Math.min(...b.pages, Infinity))},
    {label: "整册汇总", nodes: [{id: "assemble", role: "assemble", status: rendered ? "observed" : "unknown",
      title: "汇总与渲染", kicker: "Closure", code: "MERGE",
      detail: `${rendered} / ${state.total || "–"} 页 PNG 已落盘；不代替最终验收。`}]},
    {label: "整册检查", nodes: [{id: "review", role: "review", status: phase("review").status,
      title: "检查与修订", kicker: "Whole deck", code: "VERIFY", detail: phase("review").detail}]},
    {label: "最终交付", nodes: [{id: "delivery", role: "delivery", status: phase("delivery").status,
      title: "交付", kicker: "Output", code: "FINAL", detail: phase("delivery").detail}]},
  ].filter(group => group.nodes.length);
  const cardWidth = node => node.taskId ? 232
    : ({query: 180, orch: 280, blueprint: 194, assemble: 188, review: 192, delivery: 174})[node.id];
  const bandWidth = nodes => nodes.reduce((sum, node) => sum + cardWidth(node), 0) + (nodes.length - 1) * 26;
  const width = Math.max(320, viewportWidth, ...groups.map(group => bandWidth(group.nodes) + 64));
  const rows = groups.map(group => {
    let x = (width - bandWidth(group.nodes)) / 2;
    return {...group, nodes: group.nodes.map(node => {
      const positioned = {...node, x, y: 0};
      x += cardWidth(node) + 26;
      return positioned;
    })};
  });
  const edges = rows.slice(1).flatMap((row, index) => rows[index].nodes.flatMap(from =>
    row.nodes.map(to => ({from: from.id, to: to.id, axis: "vertical"}))));
  return {width, rows, edges};
}

function reconcileWorkflowNodes(stage, nodes) {
  const previous = new Map([...stage.querySelectorAll(".workflow-node")].map(card => [card.dataset.nodeId, card]));
  const cards = new Map();
  const template = document.createElement("template");
  for (const node of nodes) {
    const markup = workflowNodeMarkup(node);
    let card = previous.get(node.id);
    previous.delete(node.id);
    if (card?._workflowMarkup !== markup) {
      template.innerHTML = markup;
      const next = template.content.firstElementChild;
      if (!card) {
        card = next;
        stage.appendChild(card);
      } else {
        // 保留卡片、未变化的子区块及图片节点，轮询不重启其动画或加载。
        const ports = [...card.classList].filter(name => name.startsWith("uses-port-") || name === "dragging");
        next.classList.add(...ports);
        for (const attr of [...card.attributes]) {
          if (attr.name !== "style" && !next.hasAttribute(attr.name)) card.removeAttribute(attr.name);
        }
        for (const attr of next.attributes) {
          if (attr.name !== "style" && card.getAttribute(attr.name) !== attr.value) card.setAttribute(attr.name, attr.value);
        }
        const key = child => `${child.tagName}:${child.className}`;
        const children = new Map([...card.children].map(child => [key(child), child]));
        [...next.children].forEach((child, index) => {
          const old = children.get(key(child));
          children.delete(key(child));
          if (old?.outerHTML === child.outerHTML) child = old;
          else if (old) old.replaceWith(child);
          if (card.children[index] !== child) card.insertBefore(child, card.children[index] || null);
        });
        children.forEach(child => child.remove());
        card.style.left = next.style.left;
      }
      card._workflowMarkup = markup;
    }
    card.style.height = "";
    cards.set(node.id, card);
  }
  previous.forEach(card => card.remove());
  return cards;
}

function renderWorkflowCanvas() {
  const stage = $("#workflow-stage");
  const liveState = $("#workflow-live-state");
  if (!stage || ed.kind !== "static") return;
  renderWorkflowSidePanels();
  if (ed.workflowPane !== "graph") return;
  renderWorkflowOwnerGraph();
  if (ed.workflowOwnerAgent || ed.workflowOwnerTask) {
    if (ed.workflowOwnerAgent && ed.workflowConversationMode !== "trace") loadWorkflowAgentTrace();
    return;
  }
  const scroll = $("#workflow-scroll");
  const viewportWidth = Math.round(scroll?.clientWidth || 0);
  const viewportHeight = Math.round(scroll?.clientHeight || 0);
  const tasks = Array.isArray(ed.muralRuntime?.tasks) ? ed.muralRuntime.tasks : [];
  const graph = workflowTaskGraph(tasks, ed.agentTimings || {}, ed.feed || {}, ed.status);
  const overview = workflowOverview(graph);
  renderWorkflowOverview(overview);
  const layout = workflowFlowLayout(graph.nodes, overview, viewportWidth);
  const signature = JSON.stringify([layout, ed.status, [...ed.rendered], viewportWidth, viewportHeight,
    ed.workflowNodeOffsets, ed.workflowSelectedNodeId, ed.workflowTraceAgent, ed.workflowTraceSegment]);
  if (stage.dataset.workflowSignature === signature) return;
  stage.dataset.workflowSignature = signature;
  const {width, rows} = layout;
  const resetScroll = stage.dataset.layout !== "flow" || stage.dataset.centeredDeck !== String(ed.id);
  stage.dataset.layout = "flow";
  stage.style.width = `${width}px`;
  stage.querySelector(":scope > .workflow-empty")?.remove();
  stage.querySelector(":scope > .workflow-panel-empty")?.remove();
  const cards = reconcileWorkflowNodes(stage, rows.flatMap(row => row.nodes));
  const labels = new Map([...stage.querySelectorAll(".workflow-stage-label-row")].map(label => [label.textContent, label]));
  let y = 16;
  rows.forEach(row => {
    if (row.label) {
      let label = labels.get(row.label);
      labels.delete(row.label);
      if (!label) {
        label = document.createElement("span");
        label.className = "workflow-stage-label workflow-stage-label-row";
        label.textContent = row.label;
        stage.appendChild(label);
      }
      label.style.left = `${Math.max(20, (width - viewportWidth) / 2 + 20)}px`;
      label.style.top = `${y}px`;
      y += 30;
    }
    let height = 0;
    row.nodes.forEach(node => {
      const card = cards.get(node.id);
      card.dataset.baseY = y;
      card.style.top = `${y + (Number(ed.workflowNodeOffsets?.[node.id]?.y) || 0)}px`;
      height = Math.max(height, card.offsetHeight);
    });
    row.nodes.forEach(node => { cards.get(node.id).style.height = `${height}px`; });
    y += height + 40;
  });
  labels.forEach(label => label.remove());
  if (!graph.nodes.length) {
    stage.insertAdjacentHTML("beforeend", `<div class="workflow-panel-empty" style="position:absolute;left:20px;right:20px;top:${y}px">${Array.isArray(ed.muralRuntime?.tasks)
      ? "尚无委派任务记录；Orchestrator 的状态和 Trace 仍可查看。"
      : "接口尚未提供任务列表，请确认 WebUI 后端已更新；这不代表 Agent 没有执行。"}</div>`);
    y += 96;
  }
  stage.style.height = `${y}px`;
  stage._workflowEdges = layout.edges;
  if (scroll && resetScroll) {
    stage.dataset.centeredDeck = String(ed.id);
    requestAnimationFrame(() => {
      scroll.scrollLeft = Math.max(0, Math.round((width - scroll.clientWidth) / 2));
      scroll.scrollTop = 0;
    });
  }
  scheduleWorkflowEdgeDraw(stage);
  syncWorkflowLayoutReset();
  if (liveState) {
    const active = graph.nodes.some(node => node.status === "running") || overview.coordinator.status === "running";
    liveState.classList.toggle("running", active);
    const text = `<i></i>${active ? "任务正在执行" : "任务状态来自运行记录"}`;
    if (liveState.innerHTML !== text) liveState.innerHTML = text;
  }
}

// 顶部控制「Workflow ⇄ 实时 PPT」工作区；底部控制实时 PPT 内当前页的
// 「页面预览 ⇄ 子 Agent 消息」。两层状态必须独立，避免切换本页消息时跳回 00。
function syncViewToggle() {
  const vt = $("#view-toggle");
  const previewReady = ed.kind === "dynamic" ? !!ed.dynamicDeckUrl : !!ed.staticDeckReady;
  const isOutline = ed.sel === 0 || ed.sel == null;
  const hasPreview = !isOutline && (
    ed.kind === "dynamic" ? ed.rendered.has(ed.sel) : ed.authored.has(Number(ed.sel))
  );
  const loading = !!$("#canvas-empty > .deck-load-state");
  const processMode = loading || (ed.kind === "dynamic" && ed.workspaceView === "process");
  const workflowMode = !loading && ed.workspaceView === "workflow";
  const pageConsole = (pageViewTransitionTarget || ed.viewMode) === "console";
  const onConsole = processMode || workflowMode || !hasPreview || pageConsole;
  const editor = $("#editor");
  // 00 is a conversation/progress surface, not a 16:9 slide. Its card is
  // content-sized (with a viewport cap) so large monitors do not turn a short
  // conversation into a mostly empty full-height canvas.
  editor?.classList.toggle("outline-layout", isOutline);
  editor?.classList.toggle("preview-layout", !onConsole);
  editor?.classList.toggle("progress-layout", onConsole);
  editor?.classList.toggle("workspace-process", processMode);
  editor?.classList.toggle("workspace-workflow", workflowMode);
  editor?.classList.toggle("workspace-ppt", !processMode && !workflowMode);
  const effectiveConsole = !hasPreview || pageConsole;
  if (vt) vt.dataset.active = effectiveConsole ? "console" : "ppt";
  vt?.querySelectorAll("button").forEach((b) => {
    const isConsole = b.dataset.view === "console";
    b.classList.toggle("on", isConsole ? effectiveConsole : !effectiveConsole);
    b.disabled = !isConsole && !hasPreview;
  });
  const workspaceSwitch = $("#workspace-view-switch");
  const workspaceView = workflowMode || processMode ? "workflow" : "ppt";
  if (workspaceSwitch) workspaceSwitch.dataset.active = workspaceView;
  workspaceSwitch?.querySelectorAll("button").forEach((button) => {
    const active = button.dataset.workspaceView === workspaceView;
    const disabled = button.dataset.workspaceView === "ppt" && !previewReady;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = disabled;
    button.title = disabled ? "HTML 产出后即可查看实时 PPT" : "";
  });
  const workspaceLabel = $("#workspace-primary-label");
  if (workspaceLabel) workspaceLabel.textContent = ed.kind === "dynamic" ? "过程" : "Workflow";
  const pageEdit = $("#page-edit-open");
  if (pageEdit) {
    pageEdit.disabled = trajectoryMode || ed.kind !== "static" || ed.status !== "completed"
      || onConsole || !previewReady;
    pageEdit.title = pageEdit.disabled ? "完成后，在实时 PPT 中编辑当前页" : "修改原页面 HTML，保存并更新当前页截图";
  }
  syncPageSpeechDrawer();
  if (workflowMode) renderWorkflowCanvas();
}

function setWorkspaceView(view) {
  if (!ed.id) return;
  if ($("#canvas-empty > .deck-load-state")) return;
  const next = view === "ppt" ? "ppt" : ed.kind === "static" ? "workflow" : "process";
  const processMode = next === "process";
  const workflowMode = next === "workflow";
  const previewReady = ed.kind === "dynamic" ? !!ed.dynamicDeckUrl : !!ed.staticDeckReady;
  if (next === "ppt" && !previewReady) return;
  ed.workspaceView = next;
  setProcRailOpen(false);
  if (processMode) {
    select(0, { byUser: false });
    return;
  }
  if (workflowMode) {
    $("#canvas-empty").replaceChildren();
    if (ed.kind === "static" && !ed.liveTimer && !ed.liveFeedLoaded) fetchLive();
    syncViewToggle();
    return;
  }
  setFollow(ed.status !== "completed");
  const readyPages = ed.kind === "dynamic" ? ed.rendered : ed.authored;
  const currentReady = Number(ed.sel) > 0 && readyPages.has(Number(ed.sel));
  const target = currentReady
    ? Number(ed.sel)
    : (readyPages.size ? (ed.status === "completed" ? Math.min(...readyPages) : Math.max(...readyPages)) : 1);
  select(target, { byUser: false });
}

async function setPageView(view) {
  if (!ed.id || ed.workspaceView !== "ppt" || ed.sel === 0) return;
  const readyPages = ed.kind === "dynamic" ? ed.rendered : ed.authored;
  if (view === "ppt" && !readyPages.has(Number(ed.sel))) return;
  const nextView = view === "console" ? "console" : "ppt";
  if (ed.viewMode === nextView && pageViewTransitionTarget == null) return;
  const token = ++pageViewTransitionToken;
  pageViewTransitionTarget = nextView;
  const canvas = $("#canvas");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const direction = nextView === "console" ? 1 : -1;
  const toggle = $("#view-toggle");
  let outAnimation = null;
  if (toggle) toggle.dataset.active = nextView;

  if (!reduced && typeof canvas?.animate === "function") {
    outAnimation = canvas.animate([
      { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
      { opacity: .18, transform: `translate3d(${direction * -7}px,0,0) scale(.997)` },
    ], { duration: 115, easing: "cubic-bezier(.4,0,1,1)", fill: "both" });
    await outAnimation.finished.catch(() => {});
  }
  if (token !== pageViewTransitionToken) { outAnimation?.cancel(); return; }

  ed.viewMode = nextView;
  pageViewTransitionTarget = null;
  localStorage.setItem("studio_page_view", ed.viewMode);
  select(Number(ed.sel), { byUser: false });
  outAnimation?.cancel();

  if (!reduced && typeof canvas?.animate === "function") {
    canvas.animate([
      { opacity: .12, transform: `translate3d(${direction * 9}px,0,0) scale(.997)` },
      { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
    ], { duration: 300, easing: "cubic-bezier(.16,1,.3,1)" });
  }
}

/* ----- 画布 ----- */
let deckViewportFitRaf = 0;
let stableStaticImageToken = 0;
let dynamicDeckRevealToken = 0;
function activeCanvasSize() {
  const width = Number(ed.canvasSpec?.width_px), height = Number(ed.canvasSpec?.height_px);
  return width > 0 && height > 0 ? { width, height, ratio: width / height } : null;
}

function setCanvasSpec(spec) {
  ed.canvasSpec = spec || null;
  const size = activeCanvasSize();
  const editor = $("#editor");
  if (!size) {
    for (const key of ["--deck-aspect-ratio", "--deck-ratio", "--deck-ratio-inverse"])
      editor?.style.removeProperty(key);
    return;
  }
  editor?.style.setProperty("--deck-aspect-ratio", `${size.width} / ${size.height}`);
  editor?.style.setProperty("--deck-ratio", String(size.ratio));
  editor?.style.setProperty("--deck-ratio-inverse", String(1 / size.ratio));
}

// 预览缩放只作用于外层 iframe，作者坐标来自已经生成的真实 DOM/CSS。
function fitCanonicalStaticFrame(frame) {
  const canvas = $("#canvas");
  if (!canvas || !frame) return;
  const hostWidth = canvas.clientWidth;
  const hostHeight = canvas.clientHeight;
  if (hostWidth < 1 || hostHeight < 1) return;
  if (!activeCanvasSize()) {
    const doc = frame.contentDocument;
    const page = doc?.querySelector("#deck > .slide[data-slide]");
    const deck = page || doc?.querySelector("#deck");
    if (deck?.offsetWidth > 0 && deck?.offsetHeight > 0)
      setCanvasSpec({ width_px: deck.offsetWidth, height_px: deck.offsetHeight, source: "dom" });
  }
  const size = activeCanvasSize();
  if (!size) return;
  const { width, height } = size;
  const scale = Math.min(
    hostWidth / width,
    hostHeight / height,
  );
  const renderedWidth = width * scale;
  const renderedHeight = height * scale;
  frame.style.inset = "auto";
  frame.style.width = `${width}px`;
  frame.style.height = `${height}px`;
  frame.style.left = `${(hostWidth - renderedWidth) / 2}px`;
  frame.style.top = `${(hostHeight - renderedHeight) / 2}px`;
  frame.style.transformOrigin = "top left";
  frame.style.transform = `scale(${scale})`;
}

function resetCanonicalStaticFrame(frame) {
  for (const property of ["inset", "width", "height", "left", "top", "transformOrigin", "transform"]) {
    frame.style.removeProperty(property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`));
  }
}

// The preview column contains the current canvas and a persistent bottom
// dock. Sizing the player from width alone can make it taller than the actual
// remaining row on wide/short screens, where the sticky dock then covers the
// slide footer. Fit against both dimensions so the complete HTML canvas is
// always visible, while still consuming every available pixel on its limiting
// axis. Static present.html and dynamic deck.html share this outer geometry.
function fitWorkspaceCanvas() {
  const editor = $("#editor");
  const wrap = editor?.querySelector(".canvas-wrap");
  const canvas = $("#canvas");
  if (!editor || !wrap || !canvas) return;

  const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
  const shouldFit = !editor.hidden
    && editor.classList.contains("workspace-ppt")
    && fullscreenElement !== canvas;

  if (!shouldFit) {
    wrap.style.removeProperty("--workspace-canvas-width");
    canvas.style.removeProperty("width");
    canvas.style.removeProperty("height");
    canvas.style.removeProperty("max-width");
    canvas.style.removeProperty("max-height");
    canvas.style.removeProperty("align-self");
    return;
  }

  const wrapStyle = getComputedStyle(wrap);
  const horizontalPadding = parseFloat(wrapStyle.paddingLeft || 0) + parseFloat(wrapStyle.paddingRight || 0);
  const verticalPadding = parseFloat(wrapStyle.paddingTop || 0) + parseFloat(wrapStyle.paddingBottom || 0);
  const dock = wrap.querySelector(".canvas-bottom-dock");
  // The dock is allowed to stretch through the unused lower half of a tall
  // preview column so the speech drawer can stay pinned to its bottom edge.
  // Only reserve the dock's visible children when fitting the player;
  // subtracting the stretched outer box would collapse the player to ~1px.
  let dockHeight = 0;
  if (dock && getComputedStyle(dock).display !== "none") {
    const dockStyle = getComputedStyle(dock);
    dockHeight = parseFloat(dockStyle.paddingTop || 0) + parseFloat(dockStyle.paddingBottom || 0);
    for (const child of dock.children) {
      const childStyle = getComputedStyle(child);
      if (childStyle.display === "none" || child.hidden) continue;
      // Speaker notes occupy their own bottom area. Reserve their current
      // height so expanding the drawer scales the slide instead of covering it.
      dockHeight += child.getBoundingClientRect().height;
    }
  }
  const availableWidth = Math.max(1, wrap.clientWidth - horizontalPadding);
  const availableHeight = Math.max(1, wrap.clientHeight - verticalPadding - dockHeight);
  const size = activeCanvasSize();
  if (!size) return;
  const { ratio } = size;
  const fittedWidth = Math.min(availableWidth, availableHeight * ratio);
  const fittedHeight = fittedWidth / ratio;

  canvas.style.width = `${Math.floor(fittedWidth * 10) / 10}px`;
  canvas.style.height = `${Math.floor(fittedHeight * 10) / 10}px`;
  canvas.style.maxWidth = "100%";
  canvas.style.maxHeight = `${Math.floor(availableHeight * 10) / 10}px`;
  canvas.style.alignSelf = "center";
  wrap.style.setProperty("--workspace-canvas-width", `${Math.floor(fittedWidth * 10) / 10}px`);
}

function guardSlideDocument(slideFrame) {
  if (!slideFrame) return;
  slideFrame.setAttribute("scrolling", "no");
  try {
    const doc = slideFrame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    let style = doc.getElementById("studio-slide-viewport-guard");
    if (!style) {
      style = doc.createElement("style");
      style.id = "studio-slide-viewport-guard";
      style.textContent = `
        html, body {
          width: 100% !important;
          height: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
          margin: 0 !important;
          padding: 0 !important;
          overflow: hidden !important;
          overscroll-behavior: none !important;
          scrollbar-width: none !important;
        }
        html::-webkit-scrollbar, body::-webkit-scrollbar { display: none !important; }
        body > .slide, body > [data-slide] {
          position: absolute !important;
          inset: 0 auto auto 0 !important;
          margin: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
      `;
      doc.head?.appendChild(style);
    }
    slideFrame.contentWindow?.scrollTo(0, 0);
  } catch {
    // Older exported pages can be cross-origin; those keep their native player.
  }
}

function stabilizeStaticDeckPlayer(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    frame.setAttribute("scrolling", "no");
    doc.documentElement.style.setProperty("overflow", "hidden", "important");
    doc.body.style.setProperty("overflow", "hidden", "important");
    const deck = doc.getElementById("deck");
    const size = activeCanvasSize();
    if (size && deck?.querySelector(":scope > .slide[data-slide]")) {
      // Older inline exports omit player geometry and stack every page vertically.
      let inlineGuard = doc.getElementById("studio-static-inline-page-guard");
      if (!inlineGuard) {
        inlineGuard = doc.createElement("style");
        inlineGuard.id = "studio-static-inline-page-guard";
        doc.head?.appendChild(inlineGuard);
      }
      const css = `
        #deck {
          position: absolute !important;
          width: ${size.width}px !important;
          height: ${size.height}px !important;
          transform-origin: top left !important;
          margin: 0 !important;
          overflow: hidden !important;
        }
        #deck > .slide[data-slide] {
          position: absolute !important;
          inset: 0 auto auto 0 !important;
          margin: 0 !important;
          visibility: hidden !important;
          pointer-events: none !important;
        }
        #deck > .slide[data-slide].active {
          visibility: visible !important;
          pointer-events: auto !important;
        }
      `;
      if (inlineGuard.textContent !== css) inlineGuard.textContent = css;
    }
    let playerGuard = doc.getElementById("studio-static-player-viewport-guard");
    if (!playerGuard) {
      playerGuard = doc.createElement("style");
      playerGuard.id = "studio-static-player-viewport-guard";
      playerGuard.textContent = `
        #stage { overflow: hidden !important; }
        #wrap, #deck {
          flex: 0 0 auto !important;
          flex-shrink: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
        #wrap iframe, #deck .slide-shell iframe {
          overflow: hidden !important;
          scrollbar-width: none !important;
        }
      `;
      doc.head?.appendChild(playerGuard);
    }
    const nestedFrames = [...doc.querySelectorAll("#wrap iframe, #deck .slide-shell iframe, iframe[data-slide]")];
    nestedFrames.forEach((slideFrame) => {
      if (!slideFrame.dataset.studioViewportGuard) {
        slideFrame.dataset.studioViewportGuard = "1";
        slideFrame.addEventListener("load", () => {
          guardSlideDocument(slideFrame);
          scheduleDeckViewportFit(frame);
        });
      }
      guardSlideDocument(slideFrame);
    });
  } catch {
    // Same-origin is expected for Studio exports; legacy decks retain native behavior.
  }
}

function stabilizeDynamicDeckPlayer(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    let playerGuard = doc.getElementById("studio-dynamic-player-viewport-guard");
    if (!playerGuard) {
      playerGuard = doc.createElement("style");
      playerGuard.id = "studio-dynamic-player-viewport-guard";
      playerGuard.textContent = `
        .stage { overflow: hidden !important; }
        #deck, .stage > .deck {
          flex: 0 0 auto !important;
          flex-shrink: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
      `;
      doc.head?.appendChild(playerGuard);
    }
  } catch {
    // Same-origin is expected; third-party players keep their own fit logic.
  }
}

function fitDynamicDeckAfterLoad(frame, deckId) {
  let settled = false;
  let attempts = 0;
  const fit = () => {
    if (settled) return;
    if (frame.dataset.deckKind !== "dynamic" || frame.dataset.deckId !== String(deckId)) {
      settled = true;
      frame.removeEventListener("load", fit);
      return;
    }
    try {
      if (!frame.contentDocument?.querySelector("#deck, .deck")) return;
    } catch {
      return;
    }
    settled = true;
    frame.removeEventListener("load", fit);
    stabilizeDynamicDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    setTimeout(() => scheduleDeckViewportFit(frame), 180);
  };
  frame.addEventListener("load", fit);
  const retry = () => {
    fit();
    attempts += 1;
    if (!settled && attempts < 60) setTimeout(retry, 100);
  };
  retry();
}

function fitDeckViewport(frame = $("#canvas-deck")) {
  if (!frame || frame.hidden || !frame.contentWindow) return;
  frame.setAttribute("scrolling", "no");
  const canonicalStatic = frame.dataset.deckKind === "static"
    && editorPresentationKind() !== "dynamic";
  if (canonicalStatic) fitCanonicalStaticFrame(frame);
  else resetCanonicalStaticFrame(frame);
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    doc.documentElement.style.setProperty("overflow", "hidden", "important");
    doc.body.style.setProperty("overflow", "hidden", "important");
    frame.contentWindow.scrollTo(0, 0);
    if (frame.dataset.deckKind === "static") {
      if (editorPresentationKind() === "dynamic") stabilizeDynamicDeckPlayer(frame);
      else stabilizeStaticDeckPlayer(frame);
    }
    if (frame.dataset.deckKind === "dynamic") stabilizeDynamicDeckPlayer(frame);
    // Both canonical static decks and Dazzle decks recalculate their fixed
    // 16:9 stage from innerWidth/innerHeight on resize. Re-dispatch after the
    // iframe has actually acquired its preview/fullscreen dimensions.
    frame.contentWindow.dispatchEvent(new Event("resize"));
  } catch {
    // Cross-origin/legacy output: the iframe's own responsive rules remain active.
  }
}

function scheduleDeckViewportFit(frame = $("#canvas-deck")) {
  cancelAnimationFrame(deckViewportFitRaf);
  deckViewportFitRaf = requestAnimationFrame(() => {
    fitWorkspaceCanvas();
    fitDeckViewport(frame);
    requestAnimationFrame(() => {
      fitWorkspaceCanvas();
      fitDeckViewport(frame);
    });
  });
}

function prepareStaticDeckMotion(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement) return;
    // The Deck owns its motion tokens. Overriding them in Studio made playback
    // differ from both the renderer and the downloaded present.html.
    stabilizeStaticDeckPlayer(frame);
  } catch {
    // Same-origin is expected; legacy outputs can still fall back to their own player.
  }
}

function playStaticSlide(frame, n) {
  try {
    const deckWindow = frame.contentWindow;
    const player = deckWindow?.cleanDeck;
    if (!player?.go || (player.provisional && Number(player.count) < 1)) return false;
    const dynamicHtml = editorPresentationKind() === "dynamic";
    if (!dynamicHtml && !staticInlinePlayerIsUsable(frame, n)) return false;
    if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
    else prepareStaticDeckMotion(frame);
    player.go(n);
    // Provisional players are assembled from whatever fragments exist while a
    // deck is being authored. Some legacy skills emit complete HTML documents
    // instead of embeddable .slide[data-slide] fragments; in that case the
    // provisional shell exists but contains zero playable slides. Never let an
    // empty shell replace the stable rendered PNG.
    if (player.provisional && dynamicDeckActiveSlide(frame) !== Number(n)) return false;
    if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
    else stabilizeStaticDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    // A neighboring page can finish lazy-loading during a fast filmstrip jump.
    // Confirm that the canonical player kept the latest requested page instead
    // of exposing a stale nested iframe.
    setTimeout(() => {
      if (frame.hidden || frame.dataset.deckKind !== "static" || Number(ed.sel) !== Number(n)) return;
      const active = dynamicDeckActiveSlide(frame);
      if (active && active !== Number(n)) deckWindow.cleanDeck.go(n);
      if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
      else stabilizeStaticDeckPlayer(frame);
      scheduleDeckViewportFit(frame);
    }, 80);
    return true;
  } catch {
    return false;
  }
}

// 完整 HTML 被直接拼进片段播放器时，额外的固定舞台会覆盖所有正常页面。
// 此类产物只展示已渲染 PNG；不修改作者源码，也不将空背景误判为成功预览。
function staticInlinePlayerIsUsable(frame, n) {
  const doc = frame.contentDocument;
  const deck = doc?.getElementById("deck");
  if (!deck?.querySelector(":scope > .slide[data-slide]")) return true;
  if (![...deck.children].some(page => page.matches(".slide[data-slide]") && Number(page.dataset.slide) === Number(n))) return false;
  return ![...doc.querySelectorAll(".stage")].some(stage =>
    !stage.contains(deck) && !stage.closest(".slide[data-slide]")
  );
}

// Keep the hidden bitmap fallback on the selected page even while live HTML is
// healthy.  Previously it retained the last page until a failure path called
// keepStaticPng(); a font/load transition could therefore flash page N-1 under
// a filmstrip selection for page N.
function primeStaticPng(n) {
  if (!ed.rendered?.has(Number(n))) return;
  const img = $("#canvas-img");
  const url = fileUrl(n);
  const absoluteUrl = new URL(url, location.origin).href;
  img.dataset.slide = String(n);
  if (img.src !== absoluteUrl) img.src = url;
}

function keepStaticPng(n) {
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  frame.hidden = true;
  primeStaticPng(n);
  const url = fileUrl(n);
  const token = ++stableStaticImageToken;
  const currentIsUsable = img.complete && img.naturalWidth > 0;
  if (img.src.endsWith(url) && currentIsUsable) {
    img.hidden = false;
    empty.hidden = true;
    return;
  }

  // There is no previous bitmap to preserve on the first page selection.
  // Mount the known stable render immediately; using only a detached probe in
  // this state lets frequent progress refreshes invalidate every probe token,
  // leaving both this image and the font-pending iframe invisible.
  if (!currentIsUsable) {
    img.src = url;
    img.hidden = false;
    empty.hidden = true;
    return;
  }

  // Keep the last successfully rendered page visible while the requested page
  // is fetched. Directly replacing img.src clears the old bitmap immediately;
  // a stale id, a transient 404 or a slow AFS read would therefore turn the
  // whole preview into an empty canvas. Decode in a detached image first and
  // swap only after the new bitmap is ready.
  if (currentIsUsable) {
    img.hidden = false;
    empty.hidden = true;
  }
  const probe = new Image();
  probe.decoding = "async";
  probe.onload = async () => {
    try { await probe.decode?.(); } catch {}
    if (token !== stableStaticImageToken || ed.kind !== "static" || Number(ed.sel) !== Number(n)
        || ed.workspaceView !== "ppt" || ed.viewMode === "console"
        || (!frame.hidden && !frame.classList.contains("font-pending"))) return;
    img.src = url;
    img.hidden = false;
    empty.hidden = true;
  };
  probe.onerror = () => {
    if (token !== stableStaticImageToken || ed.kind !== "static" || Number(ed.sel) !== Number(n)) return;
    if (currentIsUsable) {
      img.hidden = false;
      empty.hidden = true;
    }
  };
  probe.src = url;
}

let staticPreviewFallbackToken = 0;
function scheduleStaticPreviewFallback(frame, n, deckId, deckStamp) {
  const token = ++staticPreviewFallbackToken;
  setTimeout(() => {
    if (token !== staticPreviewFallbackToken || ed.kind !== "static"
        || String(ed.id) !== String(deckId) || Number(ed.sel) !== Number(n)
        || frame.dataset.deckKind !== "static" || frame.dataset.deckId !== String(deckId)
        || frame.dataset.deckStamp !== String(deckStamp)
        || !frame.classList.contains("font-pending")) return;
    const img = $("#canvas-img");
    // A progress refresh can invalidate the iframe reveal callback after it
    // made the player transparent, while a stale callback has already hidden
    // the PNG. Never leave both preview layers invisible: restore the current
    // stable render and let the next selection/reload retry live HTML.
    if (img.hidden || !img.complete || img.naturalWidth < 1) keepStaticPng(n);
  }, 1500);
}

async function revealStaticDeckWhenFontsReady(frame, deckWindow) {
  const deckId = frame.dataset.deckId;
  const deckDocument = frame.contentDocument;
  const deckStamp = frame.dataset.deckStamp;
  const dynamicHtml = editorPresentationKind() === "dynamic";
  if (!deckWindow.cleanDeck && typeof deckWindow.__deckGo === "function") {
    const count = frame.contentDocument?.querySelectorAll(".slide").length || 0;
    deckWindow.cleanDeck = {
      go: (slide) => deckWindow.__deckGo(Math.max(0, Number(slide) - 1)),
      count,
      fontsReady: frame.contentDocument?.fonts?.ready || Promise.resolve(),
      senseNovaV2: true,
    };
  }
  // Static decks wait for their bundled fonts so the canonical HTML and PNG
  // remain pixel-consistent. Dynamic HTML must stay live even when a remote
  // webfont is blocked: browser fallback fonts are preferable to silently
  // replacing the deck (and all of its transitions) with a screenshot.
  if (!dynamicHtml) {
    try {
      const ready = deckWindow.cleanDeck?.fontsReady || frame.contentDocument?.fonts?.ready;
      if (ready) await ready;
    } catch {
      // A failed FontFace is checked below; preserve the stable PNG on failure.
    }
  }
  if (ed.kind !== "static" || String(ed.id) !== deckId || ed.workspaceView !== "ppt"
      || ed.viewMode === "console" || frame.dataset.deckKind !== "static"
      || frame.dataset.deckId !== deckId || frame.dataset.deckStamp !== deckStamp
      || frame.contentDocument !== deckDocument) return;
  const player = deckWindow.cleanDeck;
  const expectsSharedPreviewFonts = !!frame.contentDocument?.querySelector(
    'meta[name="studio-preview-fonts"]'
  );
  if (!dynamicHtml && expectsSharedPreviewFonts) {
    const faces = [...(frame.contentDocument?.fonts || [])];
    // document.fonts.ready also resolves when a document has no @font-face at
    // all, which previously promoted a system-fallback layout as if it matched
    // the render worker. A marked v0.4 preview must register shared faces, and
    // every face actually requested by the page must avoid the error state.
    if (!faces.length || faces.some((face) => face.status === "error")) {
      frame.dataset.fontsReady = "0";
      keepStaticPng(ed.sel);
      return;
    }
  }
  if (!player?.go || (player.provisional && Number(player.count) < 1)) {
    frame.dataset.fontsReady = "0";
    keepStaticPng(ed.sel);
    return;
  }
  frame.dataset.fontsReady = "1";
  const pending = Number(frame.dataset.pendingSlide) || ed.sel;
  if (!staticHtmlCheckpointReady(pending)) {
    frame.dataset.fontsReady = "0";
    keepStaticPng(pending);
    return;
  }
  if (!playStaticSlide(frame, pending)) {
    frame.dataset.fontsReady = "0";
    keepStaticPng(pending);
    return;
  }
  delete frame.dataset.pendingSlide;
  // A current formal present.html owns every page, so one successful load
  // closes the entire deck-wide dirty set. Keeping background pages dirty made
  // every filmstrip click navigate the iframe again and briefly expose slide 1.
  if (ed.staticDeckFinal) {
    ed.staticDirtyPages?.clear();
  } else {
    // The provisional endpoint is also a deck-wide snapshot. Clear every page
    // that was included in this load, but preserve a page re-rendered while the
    // iframe was in flight by comparing its page-specific checkpoint.
    let loadedPages = {};
    try { loadedPages = JSON.parse(frame.dataset.deckPageStampSnapshot || "{}"); } catch {}
    Object.entries(loadedPages).forEach(([page, stamp]) => {
      const current = ed.rtimes?.[page] || ed.atimes?.[page] || 0;
      if (String(current) === String(stamp)) ed.staticDirtyPages?.delete(Number(page));
    });
  }
  frame.hidden = false;
  // Position and scale the requested slide behind the stable PNG before the
  // iframe is made visible. This prevents the browser's unscaled first frame
  // and the player's default first slide from leaking into the transition.
  fitWorkspaceCanvas();
  fitDeckViewport(frame);
  requestAnimationFrame(() => {
    if (frame.dataset.deckStamp !== deckStamp) return;
    promoteStaticDeck(frame, pending, deckDocument);
  });
}

// 确认当前文档、页码与可见画布，再一起切换 HTML 和 PNG 的显示状态。
function promoteStaticDeck(frame, n, deckDocument) {
  if (ed.kind !== "static" || String(ed.id) !== frame.dataset.deckId
      || ed.workspaceView !== "ppt" || ed.viewMode === "console"
      || Number(ed.sel) !== Number(n) || frame.contentDocument !== deckDocument) return;
  frame.hidden = false;
  fitWorkspaceCanvas();
  fitDeckViewport(frame);
  if (frame.getBoundingClientRect().width < 1 || frame.getBoundingClientRect().height < 1) return;
  frame.classList.remove("font-pending");
  frame.classList.add("font-ready");
  $("#canvas-img").hidden = true;
}

function revealStaticSlideAfterLazyLoad(frame, n, deckId, attempt = 0) {
  if (ed.kind !== "static" || String(ed.id) !== String(deckId) || Number(ed.sel) !== Number(n)
      || ed.workspaceView !== "ppt" || ed.viewMode === "console"
      || frame.dataset.deckKind !== "static" || frame.dataset.deckId !== String(deckId)) return;
  if (!staticHtmlCheckpointReady(n)) {
    keepStaticPng(n);
    return;
  }
  if (playStaticSlide(frame, n)) {
    const deckDocument = frame.contentDocument;
    delete frame.dataset.pendingSlide;
    frame.hidden = false;
    fitWorkspaceCanvas();
    fitDeckViewport(frame);
    requestAnimationFrame(() => {
      promoteStaticDeck(frame, n, deckDocument);
    });
    return;
  }
  // Formal VisualCraft players lazy-load non-adjacent pages. Keep the target
  // PNG on screen while that nested iframe loads, then promote HTML in place.
  if (attempt < 80) setTimeout(() => revealStaticSlideAfterLazyLoad(frame, n, deckId, attempt + 1), 100);
}

function staticHtmlCheckpointReady(n) {
  const page = Number(n);
  if (!ed.rendered?.has(page)) return true;
  const key = String(page);
  const authoredStamp = Number(ed.atimes?.[key] || 0);
  const renderedStamp = Number(ed.rtimes?.[key] || 0);
  // Once a stable PNG exists, a newer HTML patch is only a draft. Keep the
  // checkpoint PNG until that exact page has rendered again; otherwise a
  // scaffold/patch can replace a verified visual with incomplete live DOM.
  if (!authoredStamp) return true;
  return renderedStamp > 0 && renderedStamp >= authoredStamp;
}

function showStaticDeck(n) {
  const frame = $("#canvas-deck");
  const deckId = String(ed.id);
  // Update the fallback before touching iframe visibility.  The target render
  // is normally already cached by the filmstrip, so this is effectively free
  // and removes the stale-page race entirely.
  primeStaticPng(n);
  if (!staticHtmlCheckpointReady(n)) {
    keepStaticPng(n);
    return;
  }
  const sameDeck = frame.dataset.deckKind === "static" && frame.dataset.deckId === deckId;
  // During production html_stamp is deck-wide, so another page being patched
  // must not reload the page currently on screen. Use this page's last stable
  // render checkpoint (or its first authored checkpoint before a PNG exists).
  // The final present.html promotion remains deck-wide and reloads exactly once.
  const pageKey = String(Number(n));
  const pageStamp = ed.rtimes?.[pageKey] || ed.atimes?.[pageKey] || ed.staticStamp || 0;
  const requestedStamp = String(ed.staticDeckFinal ? (ed.staticStamp || pageStamp) : pageStamp);
  const targetIsDirty = !!ed.staticDirtyPages?.has(Number(n));
  const mustReload = sameDeck && targetIsDirty && frame.dataset.deckStamp !== requestedStamp;
  frame.dataset.pendingSlide = String(n);
  $("#canvas-empty").hidden = true;
  if (sameDeck && !mustReload && frame.dataset.fontsReady === "1") {
    frame.hidden = false;
    if (playStaticSlide(frame, n)) {
      delete frame.dataset.pendingSlide;
      promoteStaticDeck(frame, n, frame.contentDocument);
    } else {
      frame.classList.add("font-pending");
      frame.classList.remove("font-ready");
      keepStaticPng(n);
      revealStaticSlideAfterLazyLoad(frame, n, deckId);
    }
    return;
  }
  // Keep a stable target-page PNG visible while a new player document loads.
  // The iframe remains mounted but transparent until revealStaticDeckWhenFontsReady
  // has selected the requested page and applied its final viewport scale.
  frame.classList.add("font-pending");
  frame.classList.remove("font-ready");
  keepStaticPng(n);
  frame.hidden = false;
  scheduleStaticPreviewFallback(frame, n, deckId, requestedStamp);
  if (sameDeck && !mustReload) {
    // The load event may have fired just before a progress refresh changed the
    // pending page. Re-run the idempotent reveal against the already loaded
    // document instead of leaving the iframe permanently transparent.
    try {
      if (frame.contentDocument?.readyState === "complete") {
        void revealStaticDeckWhenFontsReady(frame, frame.contentWindow);
      }
    } catch {}
    return;
  }
  frame.dataset.deckKind = "static";
  frame.dataset.deckId = deckId;
  frame.dataset.deckStamp = requestedStamp;
  frame.dataset.deckPageStampSnapshot = JSON.stringify(Object.fromEntries(
    [...(ed.staticDirtyPages || [])].map((page) => {
      const key = String(Number(page));
      return [key, ed.rtimes?.[key] || ed.atimes?.[key] || 0];
    }),
  ));
  frame.dataset.fontsReady = "0";
  // Do not clear every dirty page here. A deck-wide render can update many
  // pages while this iframe is loading; clearing the set made fresh thumbnails
  // coexist with stale nested slide documents. The successfully revealed page
  // is removed in revealStaticDeckWhenFontsReady(), while other pages remain
  // eligible for a cache-busted reload when selected.
  frame.src = `${ed.staticDeckUrl}?slide=${n}&t=${ed.staticStamp}`;
}

function outlineBriefData() {
  let query = String(ed.briefMeta?.user_query || ed.fullQuery || "").trim();
  const preferenceMarker = "\n\n本次演示偏好：";
  if (query.includes(preferenceMarker)) {
    [query] = query.split(preferenceMarker, 1);
  }
  const attachmentMarkers = ["\n\n【附件资料】", "\n\n【附件处理模式:pipeline_agent】"];
  let attachmentBlock = "";
  for (const marker of attachmentMarkers) {
    if (!query.includes(marker)) continue;
    [query, attachmentBlock] = query.split(marker, 2);
    break;
  }
  const attachments = (ed.attachments || []).map((item, fallbackIndex) => {
    const index = Number.isInteger(Number(item.index)) ? Number(item.index) : fallbackIndex;
    const preview = deckAttachmentPreviewUrls(item, index);
    return {
      index,
      name: item.name || item.stored_name || "附件",
      stored_name: item.stored_name || "",
      type: item.type || "file",
      size: Number(item.size) || 0,
      url: preview.url,
      fallbackUrl: preview.fallbackUrl,
      previewUrl: preview.previewUrl,
      parse: item.source_sha256 ? (ed.attachmentParse || []).find((record) =>
        record.source_sha256 === item.source_sha256) : null,
      uploadStatus: item.upload_status || "",
    };
  });
  if (!attachments.length && attachmentBlock) {
    const names = [...attachmentBlock.matchAll(/^\s*\d+\.\s*(?:文件名:\s*)?(.+?)(?:\s+\([^\n]+\)\s*->.*)?$/gm)]
      .map((match) => match[1].trim())
      .filter((name) => name && !name.startsWith("本请求"));
    names.forEach((name) => attachments.push({ name, type: name.split(".").pop() || "file", size: 0 }));
  }
  return { query: query.trim(), attachments };
}

function outlineBriefMetaHtml(data) {
  const attachments = data.attachments.length ? `<div class="outline-attachments" aria-label="本次上传的附件">
    ${data.attachments.map((item) => {
      const kind = attachmentPreviewKind(item.name, item.type);
      const icon = kind === "image" ? "▧" : (kind === "pdf" ? "PDF" : "↥");
      const parseLabel = attachmentParseLabel(item.parse, item.uploadStatus);
      const meta = [attachmentTypeLabel(item.name, item.type), attachmentSizeLabel(item.size), parseLabel].filter(Boolean).join(" · ");
      const visual = kind === "image" && item.url
        ? `<i><img src="${escapeHtml(item.url)}" alt=""${item.fallbackUrl ? ` data-attachment-image-fallback="${escapeHtml(item.fallbackUrl)}"` : ""}></i>`
        : `<i>${icon}</i>`;
      const attrs = item.url
        ? ` data-attachment-preview="1" data-attachment-url="${escapeHtml(item.url)}" data-attachment-fallback-url="${escapeHtml(item.fallbackUrl || "")}" data-attachment-preview-url="${escapeHtml(item.previewUrl || "")}" data-attachment-name="${escapeHtml(item.name)}" data-attachment-type="${escapeHtml(item.type)}" data-attachment-size="${item.size}"`
        : " disabled";
      return `<button type="button" class="outline-attachment ${kind}" title="查看 ${escapeHtml(item.name)}；解析状态不代表已阅读或语义核验"${attrs}>${visual}<span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(meta)}</small></span></button>`;
    }).join("")}
  </div>` : "";
  return attachments;
}

function attachmentParseLabel(record, uploadStatus = "") {
  if (!record) return uploadStatus === "saved" ? "原件已保存 · 等待 Harness" : uploadStatus === "not_saved" ? "上传未保存" : "";
  const label = ({queued: "等待后台解析", processing: "后台解析中", completed: "解析完成", partial: "部分解析完成", failed: "解析失败"})[record.status] || "";
  return [label, record.cached ? "复用缓存" : "", record.unresolved_count ? `${record.unresolved_count} 项未解决` : ""].filter(Boolean).join(" · ");
}

function taskComposerCopy(kind = ed.kind, status = ed.status) {
  if (kind === "dynamic" || ed.pptOutput === "dynamic_html") return {
    placeholder: "继续修改这份演示，例如：把第 3 页改得更有冲击力…",
    helper: status === "running" ? "动态演示生成中" : "继续描述修改意见",
    disabled: status === "running",
  };
  if (ed.briefMeta?.revision_supported === false) return {
    placeholder: "当前生成版本暂不支持连续编辑…",
    helper: "请使用支持静态续编的 Long-horizon / Clean 生成版本",
    disabled: true,
  };
  if (status === "waiting") return {
    placeholder: "已有一条修改要求在等待执行…",
    helper: "当前修订已排队，上一版完成后自动开始",
    disabled: true,
  };
  if (isStaticGeneratingStatus(status)) return {
    placeholder: "当前修改正在执行，完成后可继续提出要求…",
    helper: "每轮修改都会直接更新当前演示",
    disabled: true,
  };
  if (status !== "completed") return {
    placeholder: "当前版本未成功完成，无法继续修改…",
    helper: "请先重新生成一份可用成稿",
    disabled: true,
  };
  return {
    placeholder: "继续修改这份演示，例如：精简第 3 页并增强配图…",
    helper: "基于当前成稿创建可回溯的修订版本",
    disabled: false,
  };
}

function conversationDate(value) {
  if (value == null || value === "") return null;
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(Math.abs(numeric) < 1e12 ? numeric * 1000 : numeric)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function conversationDayLabel(date) {
  if (!date) return "";
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((start - target) / 86400000);
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  if (date.getFullYear() === today.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`;
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

function conversationTime(value, { withDay = false, bucketMinutes = 0 } = {}) {
  const date = conversationDate(value);
  if (!date) return null;
  const displayDate = new Date(date.getTime());
  const bucket = Number(bucketMinutes);
  if (Number.isInteger(bucket) && bucket > 1) {
    displayDate.setMinutes(Math.floor(displayDate.getMinutes() / bucket) * bucket, 0, 0);
  }
  const clock = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(displayDate);
  return {
    date,
    iso: date.toISOString(),
    groupKey: displayDate.toISOString().slice(0, 16),
    text: withDay ? `${conversationDayLabel(displayDate)} ${clock}` : clock,
  };
}

function outlineMessageTimeMarkup(value, className = "") {
  const time = conversationTime(value);
  const full = conversationTime(value, { withDay: true });
  return time && full
    ? `<time class="outline-message-time${className ? ` ${className}` : ""}" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(full.text)}">${escapeHtml(time.text)}</time>`
    : "";
}

function outlineTimeDividerMarkup(value, round) {
  const time = conversationTime(value, { withDay: true });
  if (!time) return "";
  const stage = round <= 1 ? "开始对话" : `第 ${round} 轮`;
  return `<div class="outline-time-divider" role="separator" aria-label="${escapeHtml(`${stage}，${time.text}`)}"><span></span><time datetime="${escapeHtml(time.iso)}">${escapeHtml(stage)} · ${escapeHtml(time.text)}</time><span></span></div>`;
}

function outlineUserTurnMarkup(content, { initial = false, createdAt = null, idPrefix = "outline" } = {}) {
  const text = String(content || "").trim() || (initial ? "正在读取你的演示需求…" : "");
  const displayName = $(".user-profile-copy strong")?.textContent?.trim() || "用户";
  const v1User = document.querySelector(".layout")?.dataset.authEnabled === "0";
  const avatarContent = v1User
    ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="rgba(255,255,255,.94)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8.2" r="3.25"/><path d="M5.8 19c.55-3.45 2.55-5.2 6.2-5.2s5.65 1.75 6.2 5.2"/></svg>'
    : escapeHtml($("#user-menu-trigger .user-avatar")?.textContent?.trim()
      || Array.from(displayName)[0]?.toUpperCase() || "U");
  const queryCopyId = idPrefix === "outline" ? "outline-query-copy" : `${idPrefix}-query-copy`;
  const queryId = idPrefix === "outline" ? "outline-user-query" : `${idPrefix}-user-query`;
  const queryMetaId = idPrefix === "outline" ? "outline-query-meta" : `${idPrefix}-query-meta`;
  const copy = `<button type="button" class="outline-query-copy"${initial ? ` id="${queryCopyId}"` : ""} data-copy-message="${escapeHtml(text)}" title="复制用户消息" aria-label="复制用户消息">复制</button>`;
  const meta = initial ? `<div id="${queryMetaId}">${outlineBriefMetaHtml(outlineBriefData())}</div>` : "";
  return `<section class="outline-user-turn${initial ? " initial" : " followup"}">
    <div class="outline-user-identity"><span class="outline-turn-label">${escapeHtml(displayName)}</span><span class="outline-user-avatar${v1User ? " outline-user-avatar-v1" : ""}" aria-hidden="true">${avatarContent}</span></div>
    <div class="outline-user-bubble"><p${initial ? ` id="${queryId}"` : ""}>${escapeHtml(text)}</p>${meta}</div>
    <div class="outline-user-actions">${outlineMessageTimeMarkup(createdAt)}${copy}</div>
  </section>`;
}

function skillProvenanceMarkup(prov) {
  // 固定版本不等于已注入；实际上下文加载记录保留在 Trace 中。
  if (!prov || typeof prov !== "object") return "";
  const main = prov.main_sha256
    ? `<div class="skill-prov-row"><span class="skill-prov-ok" aria-hidden="true">✓</span>
        <div><b>已固定主 Skill 版本</b><code>${escapeHtml(prov.main_path || "SKILL.md")} · ${escapeHtml(prov.main_sha256)}</code></div></div>`
    : "";
  const roles = Array.isArray(prov.roles) ? prov.roles.filter((r) => r && r.sha256) : [];
  const roleRows = roles.length
    ? `<div class="skill-prov-row"><span class="skill-prov-ok" aria-hidden="true">✓</span>
        <div><b>可用角色卡 · 不代表已启动</b>${roles.map((r) =>
          `<code>${escapeHtml(r.path || `roles/${r.role}.md`)} · ${escapeHtml(r.sha256)}</code>`).join("")}</div></div>`
    : "";
  if (!main && !roleRows) return "";
  const src = prov.source ? `<div class="skill-prov-src">来源 ${escapeHtml(prov.source)}</div>` : "";
  return `<div class="skill-provenance" id="skill-provenance">${main}${roleRows}${src}</div>`;
}

function outlineTaskConfigMarkup(kind, config) {
  const provenance = kind === "dynamic" ? "" : skillProvenanceMarkup(ed.skillProvenance);
  return `<details class="outline-task-config" id="outline-task-config" data-kind="${kind}">
    <summary>
      <span class="outline-config-mark" aria-hidden="true">${agentIdentityGlyph(kind, "outline-config-glyph")}</span>
      <b>本次生成配置</b>
      <i id="outline-config-summary">${escapeHtml(`${config.model} · ${config.skill} · 深度思考：${config.thinking === "开启" ? "已开启" : "未开启"}`)}</i>
      <em aria-hidden="true">⌄</em>
    </summary>
    <div class="outline-config-body">
      <div><span>模型</span><b id="outline-config-model">${escapeHtml(config.model)}</b></div>
      <div><span>Skill</span><b id="outline-config-skill">${escapeHtml(config.skill)}</b></div>
      <div><span>深度思考</span><b id="outline-config-thinking">${escapeHtml(config.thinking)}</b></div>
      <div><span>Agent 上限</span><b id="outline-config-runtime">${escapeHtml(config.runtime)}</b></div>
      <div id="outline-config-pref-row"${config.preferences ? "" : " hidden"}><span>偏好</span><b id="outline-config-pref">${escapeHtml(config.preferences || "—")}</b></div>
    </div>${provenance}
  </details>`;
}

function outlineAssistantSummary(turn) {
  const revision = Number(turn?.revision_no) || 0;
  const pages = Number(turn?.slide_count) || 0;
  const status = String(turn?.status || "completed");
  if (status === "completed") return {
    title: revision ? "已按要求完成修改" : "初版演示已经完成",
    detail: pages ? `已完成 ${pages} 页演示，可以继续提出修改意见。` : "演示已经完成，可以继续提出修改意见。",
    state: "completed",
  };
  if (["failed", "rejected", "error"].includes(status)) return {
    title: revision ? "本轮修改未能完成" : "初版演示未能完成",
    detail: "可以调整要求后重新尝试。", state: "failed",
  };
  return { title: revision ? "正在处理这条修改" : "正在制作演示", detail: "MURAL Presenter 正在继续推进。", state: "running" };
}

function dynamicAssistantSummary(turn) {
  const logs = turn?.logs || [];
  const meaningful = [...logs].reverse().find((entry) => ["final", "text", "note"].includes(entry.kind) && entry.text)?.text;
  const toolCount = logs.filter((entry) => entry.kind === "tool").length;
  const completed = turn?.status === "completed";
  const compact = meaningful ? compactPageAgentResult(meaningful, { completed }).summary : "";
  return {
    title: completed ? "本轮制作已经完成" : (turn?.status === "error" ? "本轮制作未能完成" : "已完成上一阶段处理"),
    detail: compact || (toolCount ? `本轮共推进 ${toolCount} 项制作步骤。` : "演示内容已根据你的要求完成处理。"),
    state: completed ? "completed" : (turn?.status === "error" ? "failed" : "completed"),
  };
}

function dynamicHistoricalProcessMarkup(turn) {
  const label = { tool: "制作步骤", text: "Agent 回复", note: "进展说明", final: "最终回复", error: "异常记录" };
  const seen = new Set();
  const entries = (turn?.logs || []).flatMap((entry, index) => {
    const kind = String(entry?.kind || "text");
    if (!Object.prototype.hasOwnProperty.call(label, kind)) return [];
    const text = cleanAgentText(entry?.text);
    if (!text) return [];
    const fingerprint = `${kind}\u0000${text}`;
    if (seen.has(fingerprint)) return [];
    seen.add(fingerprint);
    return [{ kind, text, index }];
  });
  const rows = entries.length ? entries.map((entry) => `<article class="dynamic-history-entry ${entry.kind}">
      <span class="dynamic-history-mark" aria-hidden="true"></span>
      <div><b>${label[entry.kind]}</b><div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div></div>
    </article>`).join("") : '<div class="outline-history-unavailable">这一轮暂时没有可展示的详细记录。</div>';
  return `<div class="outline-agent-history-process dynamic-history-process">
      <div class="dynamic-history-head"><b>本轮完整制作过程</b><span>${entries.length} 条记录</span></div>
      <div class="dynamic-history-list">${rows}</div>
    </div>`;
}

function outlineHistoricalAgentMarkup(turn, kind) {
  const summary = kind === "dynamic" ? dynamicAssistantSummary(turn) : outlineAssistantSummary(turn);
  const revision = Math.max(0, Number(turn?.revision_no) || 0);
  const process = kind === "static" ? `<div class="outline-agent-history-process">
        <div class="outline-agent-progress outline-agent-history-progress" data-revision="${revision}">
          <div class="outline-history-loading"><i></i><span>正在载入这一轮的完整制作过程…</span></div>
        </div>
      </div>` : dynamicHistoricalProcessMarkup(turn);
  return `<div class="outline-thread-bridge" aria-hidden="true"><span></span></div>
    <section class="outline-agent-turn outline-agent-history ${summary.state}">
      <div class="outline-agent-label"><span class="outline-agent-mark">${agentIdentityGlyph(kind)}</span><span class="outline-agent-copy"><b>MURAL Presenter</b><i>上一轮回复</i></span>${outlineMessageTimeMarkup(turn?.responded_at || turn?.created_at, "agent")}</div>
      <details class="outline-agent-history-details">
        <summary class="outline-agent-history-card"><span class="outline-history-state" aria-hidden="true"></span><div><strong>${escapeHtml(summary.title)}</strong><p>${escapeHtml(summary.detail)}</p></div><i class="outline-history-chevron" aria-hidden="true"></i></summary>
        ${process}
      </details>
    </section>`;
}

function outlineCurrentAgentMarkup(kind, running, turn = {}, { progressId = "agent-progress-console" } = {}) {
  const responseLabel = ed.status === "not_started" ? "等待开始" : (running ? "正在执行任务" : "本次任务回复");
  return `<div class="outline-thread-bridge" aria-hidden="true"><span></span></div>
    <section class="outline-agent-turn">
      <div class="outline-agent-label"><span class="outline-agent-mark">${agentIdentityGlyph(kind)}</span><span class="outline-agent-copy"><b>MURAL Presenter</b><i>${responseLabel}</i></span>${outlineMessageTimeMarkup(turn?.responded_at || turn?.created_at, "agent")}</div>
      <div class="outline-agent-progress" id="${escapeHtml(progressId)}"></div>
    </section>`;
}

function outlineConversationTurns(kind, data) {
  let turns = kind === "dynamic" ? (ed.dynamicChatTurns || []) : (ed.staticConversationTurns || []);
  if (!turns.length) {
    turns = [{ role: "user", content: data.query || "" }];
    const followups = kind === "dynamic" ? (ed.dynamicFollowups || []) : (ed.staticFollowups || []);
    followups.forEach((content) => {
      turns.push({ role: "assistant", status: "completed", slide_count: ed.total || ed.briefMeta?.slide_count || 0 });
      turns.push({ role: "user", content });
    });
    turns.push({ role: "assistant", current: true, status: ed.status });
  }
  return turns.map((turn, index) => index === 0 && turn.role === "user"
    ? { ...turn, content: data.query || turn.content }
    : turn);
}

function outlineConversationLaneMarkup(kind, running, { idPrefix = "outline", progressId = "agent-progress-console" } = {}) {
  const data = outlineBriefData();
  const turns = outlineConversationTurns(kind, data);
  let currentRendered = false;
  let round = 0;
  const html = turns.map((turn, index) => {
    if (turn.role === "user") {
      const initial = index === 0;
      round += 1;
      return outlineTimeDividerMarkup(turn.created_at, round)
        + outlineUserTurnMarkup(turn.content, { initial, createdAt: turn.created_at, idPrefix });
    }
    if (turn.current || (index === turns.length - 1 && turn.role === "assistant")) {
      currentRendered = true;
      return outlineCurrentAgentMarkup(kind, running, turn, { progressId });
    }
    return outlineHistoricalAgentMarkup(turn, kind);
  }).join("");
  return html + (currentRendered ? "" : outlineCurrentAgentMarkup(kind, running, {}, { progressId }));
}

function outlineConversationStructureKey(kind) {
  const brief = outlineBriefData();
  return JSON.stringify({ attachments: brief.attachments, turns: outlineConversationTurns(kind, brief).map((turn) => ({
    role: turn.role, content: turn.content, deck_id: turn.deck_id, status: turn.status,
    revision_no: turn.revision_no, slide_count: turn.slide_count, current: !!turn.current,
    created_at: turn.created_at, responded_at: turn.responded_at,
  })) });
}

function syncOutlineConversationTurns() {
  const lane = $("#outline-chat-lane");
  if (!lane) return;
  const displayKind = editorPresentationKind();
  const key = outlineConversationStructureKey(displayKind);
  if (lane.dataset.turnKey !== key) {
    lane.dataset.turnKey = key;
    const running = ed.kind === "dynamic" ? ed.status === "running" : isStaticActiveStatus(ed.status);
    lane.innerHTML = outlineConversationLaneMarkup(displayKind, running);
  }
  syncTaskConfig();
  if (displayKind === "static") syncStaticHistoricalTurnFeeds();
}

async function syncStaticHistoricalTurnFeeds() {
  if (ed.kind !== "static" || !ed.id) return;
  const nodes = $$(".outline-agent-history-progress[data-revision]");
  await Promise.all(nodes.map(async (node) => {
    if (node.dataset.feedState === "loading" || node.dataset.feedState === "ready") return;
    node.dataset.feedState = "loading";
    const revision = Number(node.dataset.revision) || 0;
    try {
      const payload = await jget(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}/turn-feed?revision_no=${revision}`);
      if (ed.kind !== "static" || !node.isConnected) return;
      node.dataset.feedState = "ready";
      const workflowList = node.closest("#workflow-message-list");
      renderOrchestrationProgress(node, payload.agents || {}, {
        running: false,
        specialistArtifacts: payload.specialist_artifacts || {},
        artifactMode: workflowList ? "links" : "inline",
        scrollContainer: workflowList || null,
      });
      if (workflowList) syncWorkflowArtifactSelection();
    } catch {
      if (!node.isConnected) return;
      node.dataset.feedState = "error";
      node.innerHTML = '<div class="outline-history-unavailable">这一轮的归档过程暂不可用，任务结果仍保留在会话中。</div>';
    }
  }));
}

function taskComposerMarkup(kind, status) {
  const copy = taskComposerCopy(kind, status);
  const draft = taskComposerDrafts.get(taskComposerKey(kind, ed.id)) || "";
  return `<div class="task-composer-wrap" data-kind="${kind}">
    <form class="task-composer" id="task-composer" autocomplete="off">
      <div class="task-composer-main">
        <textarea id="task-composer-input" rows="1" maxlength="8000" placeholder="${escapeHtml(copy.placeholder)}">${escapeHtml(draft)}</textarea>
        <button type="submit" id="task-composer-send" aria-label="发送修改要求" title="${copy.disabled ? escapeHtml(copy.helper) : "发送修改要求"}"${copy.disabled ? " disabled" : ""}>
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 15.5V4.5M5.5 9 10 4.5 14.5 9"/></svg>
        </button>
      </div>
      <div class="task-composer-foot">
        <span class="task-composer-helper"><i></i><em>${escapeHtml(copy.helper)}</em></span>
        <span class="task-composer-shortcut">⌘/Ctrl + Enter</span>
      </div>
    </form>
  </div>`;
}

function resizeGrowingTextarea(input, { maxHeight = 236, viewportRatio = .32 } = {}) {
  if (!input) return;
  cancelAnimationFrame(input.__growFrame);
  input.__growFrame = requestAnimationFrame(() => {
    // offsetHeight is expressed in unzoomed CSS layout pixels. The Studio UI
    // uses body zoom, while getBoundingClientRect() returns the zoomed visual
    // height; comparing the latter with scrollHeight made every keystroke look
    // like a height change and continuously restarted the row animation.
    const currentHeight = input.offsetHeight;
    const minimum = Number.parseFloat(getComputedStyle(input).minHeight) || 0;
    const viewportLimit = Math.max(minimum, Math.floor(window.innerHeight * viewportRatio));
    const limit = Math.max(minimum, Math.min(maxHeight, viewportLimit));
    const inlineTransition = input.style.transition;
    input.style.transition = "none";
    input.style.height = "auto";
    const naturalHeight = input.scrollHeight;
    const targetHeight = Math.max(minimum, Math.min(naturalHeight, limit));
    const startHeight = currentHeight || minimum;
    const heightChanged = Math.abs(targetHeight - startHeight) > .5;
    input.style.height = `${heightChanged ? startHeight : targetHeight}px`;
    if (heightChanged) input.offsetHeight;
    if (inlineTransition) input.style.transition = inlineTransition;
    else input.style.removeProperty("transition");
    if (heightChanged) input.style.height = `${targetHeight}px`;
    input.style.overflowY = naturalHeight > limit + 1 ? "auto" : "hidden";
    input.classList.toggle("is-expanded", targetHeight > minimum + 8);
    if (input.id === "q") {
      clearTimeout(input.__growClampTimer);
      input.__growClampTimer = setTimeout(() => clampPrimaryComposerToViewport(input, naturalHeight, minimum), 270);
    }
  });
}

function clampPrimaryComposerToViewport(input, naturalHeight, minimum) {
  const dock = input?.closest(".composer-dock");
  const workspace = input?.closest(".creation-workspace");
  if (!dock || !workspace) return;
  const workspaceBottom = Math.min(window.innerHeight, workspace.getBoundingClientRect().bottom);
  const overflow = dock.getBoundingClientRect().bottom - workspaceBottom + 12;
  if (overflow <= 0) return;
  const visualHeight = input.getBoundingClientRect().height;
  const layoutHeight = input.offsetHeight;
  const visualScale = layoutHeight > 0 ? visualHeight / layoutHeight : 1;
  const correctedHeight = Math.max(minimum, layoutHeight - overflow / Math.max(visualScale, .01));
  input.style.height = `${correctedHeight}px`;
  input.style.overflowY = naturalHeight > correctedHeight + 1 ? "auto" : "hidden";
  input.classList.toggle("is-expanded", correctedHeight > minimum + 8);
}

function resizeTaskComposerInput(input) {
  resizeGrowingTextarea(input, { maxHeight: 196, viewportRatio: .26 });
}

function resizePrimaryComposerInput(input = $("#q")) {
  resizeGrowingTextarea(input, { maxHeight: 176, viewportRatio: .26 });
}

function resetGrowingTextarea(input) {
  if (!input) return;
  cancelAnimationFrame(input.__growFrame);
  input.style.height = "auto";
  input.style.overflowY = "hidden";
  input.classList.remove("is-expanded");
  resizeGrowingTextarea(input, input.id === "q"
    ? { maxHeight: 176, viewportRatio: .26 }
    : { maxHeight: 196, viewportRatio: .26 });
}

function syncTaskComposerState() {
  const form = $("#task-composer");
  if (!form || ed.sel !== 0) return;
  const copy = taskComposerCopy(ed.kind, ed.status);
  const input = $("#task-composer-input");
  const button = $("#task-composer-send");
  const helper = form.querySelector(".task-composer-helper em");
  input.placeholder = copy.placeholder;
  button.disabled = copy.disabled || form.classList.contains("is-sending");
  button.title = copy.disabled ? copy.helper : "发送修改要求";
  if (helper.textContent !== copy.helper) helper.textContent = copy.helper;
  form.closest(".task-composer-wrap")?.classList.toggle("is-locked", copy.disabled);
}

function bindTaskComposer() {
  const form = $("#task-composer");
  const input = $("#task-composer-input");
  if (!form || !input || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  resizeTaskComposerInput(input);
  input.addEventListener("input", () => {
    taskComposerDrafts.set(taskComposerKey(), input.value);
    resizeTaskComposerInput(input);
  });
  input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      if (!$("#task-composer-send")?.disabled) form.requestSubmit();
    }
  });
  form.addEventListener("submit", sendTaskFollowup);
  syncTaskComposerState();
}

async function sendTaskFollowup(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const input = $("#task-composer-input");
  const message = input?.value.trim();
  if (!message) return;
  if (taskComposerCopy(ed.kind, ed.status).disabled) return;
  if (!requireAuthentication()) return;
  form.classList.add("is-sending");
  syncTaskComposerState();
  const helper = form.querySelector(".task-composer-helper em");
  helper.textContent = "正在发送修改要求…";
  try {
    const isDynamic = ed.kind === "dynamic";
    const response = await fetch(
      isDynamic ? `${globalThis.STUDIO_BASE_PATH || ""}/api/dynamic/send` : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/continue`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isDynamic ? { conv_id: String(ed.id), message } : { message }),
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || "修改要求发送失败");
    taskComposerDrafts.delete(taskComposerKey());
    input.value = "";
    resetGrowingTextarea(input);
    if (isDynamic) {
      ed.status = "running";
      ed.dynamicPhase = "render";
      subscribeDynamic(String(ed.id));
      startDynamicConversationPoll(String(ed.id));
      if (!ed.timer) ed.timer = setInterval(tick, 1000);
      renderDynamicChrome();
    } else {
      const revisionId = Number(payload.deck_id);
      await loadDecks(revisionId);
      await openDeck(revisionId);
    }
  } catch (error) {
    alert("发送失败: " + error.message);
  } finally {
    form.classList.remove("is-sending");
    syncTaskComposerState();
  }
}

function agentIdentityGlyph(kind = ed.kind, className = "agent-identity-glyph") {
  if (kind === "static") {
    return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect x="3" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    </svg>`;
  }
  return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <path d="M10 2.7c.7 3.56 3.06 5.92 6.62 6.62-3.56.7-5.92 3.06-6.62 6.62-.7-3.56-3.06-5.92-6.62-6.62C6.94 8.62 9.3 6.26 10 2.7Z" fill="currentColor"/>
  </svg>`;
}

function syncOutlineBrief() {
  const query = $("#outline-user-query");
  const meta = $("#outline-query-meta");
  if (!query || !meta) return;
  const data = outlineBriefData();
  const queryText = data.query || "正在读取你的演示需求…";
  if (query.textContent !== queryText) query.textContent = queryText;
  const metaHtml = outlineBriefMetaHtml(data);
  if (meta.dataset.renderKey !== metaHtml) {
    meta.dataset.renderKey = metaHtml;
    meta.innerHTML = metaHtml;
  }
}

function outlineConversationMarkup({ kind, running }) {
  const dynamic = kind === "dynamic";
  const mode = dynamic ? "动态演示" : "静态演示";
  return `
    <div class="ce-top workstream-head outline-head${dynamic ? " dynamic" : ""}">
      <span class="big-no" aria-hidden="true">00</span>
      <div class="ce-meta">
        <div class="outline-head-copy"><div class="ce-text">任务会话与整体规划</div><div class="ce-sub">从需求理解到演示结构，关键进展会持续更新</div></div>
        <span class="outline-mode-pill"><i></i>${mode}</span>
        ${running ? '<div class="develop-ring mini" aria-label="正在制作"></div>' : ""}
      </div>
    </div>
    <div class="term workstream outline-conversation${running ? "" : " static"}" data-kind="${kind}">
      <div class="term-body outline-chat-body" id="gen-console">
        <div class="outline-chat-lane" id="outline-chat-lane" data-turn-key="${escapeHtml(outlineConversationStructureKey(kind))}">${outlineConversationLaneMarkup(kind, running)}</div>
      </div>
      ${taskComposerMarkup(kind, ed.status)}
    </div>`;
}

function workstreamMarkup({ n, kind, running, rendered = false }) {
  const outline = n === 0;
  if (outline) return outlineConversationMarkup({ kind, running });
  const dynamic = kind === "dynamic";
  const title = dynamic
    ? `第 ${pad2(n)} 页 · ${running && !rendered ? "正在编排页面与动效" : "页面与动效制作记录"}`
    : `第 ${pad2(n)} 页 · ${running && !rendered ? "正在制作内容与画面" : "内容与画面制作记录"}`;
  const agent = dynamic
    ? "正在持续完善页面布局、动效与呈现"
    : "正在制作本页内容与视觉";
  const state = running && !rendered ? "正在制作" : (rendered ? "页面已完成" : "工作记录");
  return `
    <div class="ce-top workstream-head">
      <span class="big-no">${pad2(n)}</span>
      <div class="ce-meta">
        <div><div class="ce-text">${title}<span class="ce-task" id="ce-task"></span></div><div class="ce-sub" id="ce-agent">${agent}</div></div>
        ${running && !rendered ? '<div class="develop-ring mini" aria-label="正在制作"></div>' : ""}
      </div>
    </div>
    <div class="term workstream${running ? "" : " static"}" data-kind="${kind}">
      <div class="term-bar"><span class="workstream-mark">✦</span><span class="term-title" id="term-title">${dynamic ? "本页动态制作进展" : "本页制作进展"}</span><span class="workstream-status"><i></i>${state}</span></div>
      <div class="term-body" id="gen-console"><div class="lv-caret"><i></i><span>${running ? "AI 正在推进当前任务" : "以上为本页制作记录"}</span></div></div>
    </div>`;
}

function select(n, { byUser } = {}) {
  if (ed.kind === "dynamic") return selectDynamic(n, { byUser });
  if (byUser) setFollow(false);
  ed.sel = n;
  setRailPageScope(n);
  if (n > 0) loadPageHistory(n);
  markSel();
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  if (n === 0) {
    const running = isStaticActiveStatus(ed.status);
    ed.workspaceView = "workflow";
    img.hidden = true; frame.hidden = true; empty.replaceChildren();
    ed.conKey = null; ed.conLines = [];
    if (!running && !ed.liveTimer) fetchLive();   // 已完成的 deck:拉一次历史输出
    syncViewToggle();
    loadPlan(0);
    updateNav();
    return;
  }
  const showConsole = ed.viewMode === "console";
  if (ed.authored.has(n) && !showConsole) {
    if (ed.staticDeckReady && ed.staticDeckUrl) {
      // 单页 HTML 完整写入后立即显示实时页面；PNG 仅作为载入失败回退。
      showStaticDeck(n);
    } else if (ed.rendered.has(n)) {
      keepStaticPng(n);
    }
  } else {
    // 制作进度模式或页面尚未渲染：展示语义化 AI 工作流，而非底层命令行。
    const running = isStaticActiveStatus(ed.status);
    const rendered = ed.rendered.has(n);
    img.hidden = true; frame.hidden = true; empty.hidden = false;
    empty.innerHTML = workstreamMarkup({ n, kind: "static", running, rendered });
    ed.conKey = null; ed.conLines = [];
    if (!running && !ed.liveTimer && !Object.keys(ed.feed || {}).length) fetchLive();
    else renderLive();
  }
  syncViewToggle();
  loadPlan(n);
  updateNav();
}

function selectDynamic(n, { byUser } = {}) {
  if (byUser) setFollow(false);
  ed.sel = n; markSel();
  setRailPageScope(n);
  if (n > 0) loadDynamicPageSpeech(n);
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  const rendered = ed.rendered.has(n);
  const showDeck = n > 0 && rendered && ed.dynamicDeckUrl && ed.viewMode !== "console";
  if (showDeck) {
    const deckId = String(ed.id);
    const requestedStamp = String(ed.dynamicStamp || 0);
    const sameDeck = frame.dataset.deckKind === "dynamic" && frame.dataset.deckId === deckId;
    const sameArtifact = sameDeck && frame.dataset.deckStamp === requestedStamp;
    if (sameArtifact && frame.dataset.deckReady === "1" && playDynamicSlide(frame, n)) {
      frame.hidden = false;
      img.hidden = true;
      empty.hidden = true;
      $("#plan-panel").hidden = true;
      syncViewToggle(); updateNav();
      return;
    }
    frame.dataset.pendingSlide = String(n);
    frame.dataset.revealToken = String(++dynamicDeckRevealToken);
    // Keep the live HTML mounted but fully transparent while it positions the
    // requested page. Do not pass through a rendered PNG between live states.
    img.hidden = true;
    empty.hidden = true;
    frame.hidden = false;
    frame.classList.add("font-pending");
    frame.classList.remove("font-ready");
    // The iframe is still loading. Its load handler will apply the latest
    // pending page through __deckGo, so rapid thumbnail clicks stay smooth.
    if (sameArtifact && ["0", "revealing"].includes(frame.dataset.deckReady)) {
      if (typeof frame.contentWindow?.__deckGo === "function") revealDynamicDeckSlide(frame, n);
      $("#plan-panel").hidden = true;
      syncViewToggle(); updateNav();
      return;
    }
    frame.dataset.deckKind = "dynamic";
    frame.dataset.deckId = deckId;
    frame.dataset.deckStamp = requestedStamp;
    frame.dataset.deckReady = "0";
    const src = `${ed.dynamicDeckUrl}?slide=${n}&t=${requestedStamp}`;
    fitDynamicDeckAfterLoad(frame, ed.id);
    if (!frame.src.endsWith(src)) frame.src = src;
  } else {
    frame.hidden = true; empty.hidden = false;
    const running = ed.status === "running";
    empty.innerHTML = workstreamMarkup({ n, kind: "dynamic", running, rendered });
    if (n === 0) bindTaskComposer();
    renderDynamicConsole();
  }
  $("#plan-panel").hidden = true;
  syncViewToggle(); updateNav();
}

// iframe 内自行翻页时，同步主编辑器页码与左侧胶片条。动态 Dazzle Deck 与静态
// present.html 都派发 slidechange；同时读 active slide，兼容无 detail 的旧 Deck。
function dynamicDeckActiveSlide(frame) {
  try {
    const active = frame.contentDocument?.querySelector(".slide.active, [data-slide].active");
    if (!active) return 0;
    const explicit = Number(active.dataset.slide);
    if (Number.isInteger(explicit) && explicit > 0) return explicit;
    const slides = [...frame.contentDocument.querySelectorAll(".slide, [data-slide]")];
    const index = slides.indexOf(active);
    return index >= 0 ? index + 1 : 0;
  } catch {
    return 0;
  }
}

// Dynamic Skills historically kept their navigator (setSlide/goto) inside an
// IIFE, so Studio could not call it from the parent page. Reloading deck.html
// with a different ?slide= value worked, but tore down the live canvas/Three.js
// scene and exposed the iframe background between every filmstrip click. Add a
// same-origin bridge for those exports so page changes happen in the already
// painted document. Native __deckGo implementations remain authoritative.
function installDynamicDeckAdapter(frame) {
  try {
    const deckWindow = frame?.contentWindow;
    const doc = frame?.contentDocument;
    if (!deckWindow || !doc?.documentElement) return false;
    if (typeof deckWindow.__deckGo === "function") return true;
    const slides = [...doc.querySelectorAll(".slide, [data-slide]")]
      .filter((slide, index, all) => !all.some((other) => other !== slide && other.contains(slide)));
    if (!slides.length) return false;

    deckWindow.__deckGo = (requestedIndex) => {
      const target = Math.max(0, Math.min(slides.length - 1, Number(requestedIndex) || 0));
      slides.forEach((slide, index) => {
        slide.classList.toggle("active", index === target);
        slide.classList.toggle("prev", index < target);
      });

      const page = target + 1;
      const ratio = page / slides.length;
      const pageCur = doc.getElementById("pageCur");
      const pageNo = doc.getElementById("pageNo");
      const pageTotal = doc.getElementById("pageTotal");
      if (pageCur) pageCur.textContent = String(page).padStart(2, "0");
      if (pageNo) pageNo.textContent = String(page).padStart(2, "0");
      if (pageTotal) pageTotal.textContent = String(slides.length).padStart(2, "0");
      const progress = doc.getElementById("progressBar");
      if (progress) {
        // Export variants use either a full-width <i> scaled on X or a bar
        // whose width is animated. Combining both would square the progress.
        if (progress.tagName === "I") progress.style.transform = `scaleX(${ratio})`;
        else progress.style.width = `${ratio * 100}%`;
      }

      try { deckWindow.__tweenTo?.(target); } catch {}
      try { deckWindow.slideInits?.[page]?.(); } catch {}
      deckWindow.dispatchEvent(new deckWindow.CustomEvent("slidechange", { detail: { index: page } }));
    };
    deckWindow.__studioDeckAdapter = true;
    return true;
  } catch {
    return false;
  }
}

// Newer Dazzle/Cinescale decks may expose a native zero-based navigator as
// window.__deckGo; installDynamicDeckAdapter supplies the same contract for
// legacy exports. Reusing the live document is essential: changing iframe.src
// destroys the previous slide before CSS can animate the outgoing/incoming pair.
function playDynamicSlide(frame, n) {
  try {
    const deckWindow = frame.contentWindow;
    if (!installDynamicDeckAdapter(frame) || typeof deckWindow?.__deckGo !== "function") return false;
    deckWindow.__deckGo(Math.max(0, Number(n) - 1));
    stabilizeDynamicDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    setTimeout(() => {
      if (frame.hidden || frame.dataset.deckKind !== "dynamic" || Number(ed.sel) !== Number(n)) return;
      const active = dynamicDeckActiveSlide(frame);
      if (active && active !== Number(n)) deckWindow.__deckGo(Math.max(0, Number(n) - 1));
      stabilizeDynamicDeckPlayer(frame);
      scheduleDeckViewportFit(frame);
    }, 80);
    return true;
  } catch {
    return false;
  }
}

// Apply the pending page while the iframe is still transparent, verify its
// active slide, then reveal the already-correct live player directly.
// The token makes rapid filmstrip clicks cancel stale reveal callbacks.
function revealDynamicDeckSlide(frame, n) {
  const target = Number(n);
  const token = frame.dataset.revealToken;
  const deckId = frame.dataset.deckId;
  const deckStamp = frame.dataset.deckStamp;
  const deckWindow = frame.contentWindow;
  if (!target || !token || typeof deckWindow?.__deckGo !== "function") return false;
  frame.dataset.deckReady = "revealing";
  frame.classList.add("font-pending");
  frame.classList.remove("font-ready");

  const currentRequestIsValid = () => (
    ed.kind === "dynamic"
    && String(ed.id) === deckId
    && Number(ed.sel) === target
    && frame.dataset.deckKind === "dynamic"
    && frame.dataset.deckId === deckId
    && frame.dataset.deckStamp === deckStamp
    && frame.dataset.revealToken === token
    && Number(frame.dataset.pendingSlide) === target
    && frame.contentWindow === deckWindow
  );
  let attempts = 0;
  const positionThenReveal = () => {
    if (!currentRequestIsValid()) return;
    try { deckWindow.__deckGo(Math.max(0, target - 1)); } catch { return; }
    stabilizeDynamicDeckPlayer(frame);
    const active = dynamicDeckActiveSlide(frame);
    attempts += 1;
    if (active !== target && attempts < 10) {
      setTimeout(positionThenReveal, 24);
      return;
    }
    if (active !== target) {
      // Do not reveal an unverified page. A future load/selection can retry
      // without exposing the deck's initial page 1.
      frame.dataset.deckReady = "0";
      return;
    }
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!currentRequestIsValid() || dynamicDeckActiveSlide(frame) !== target) {
        if (currentRequestIsValid()) setTimeout(positionThenReveal, 24);
        return;
      }
      delete frame.dataset.pendingSlide;
      frame.dataset.deckReady = "1";
      frame.hidden = false;
      $("#canvas-empty").hidden = true;
      scheduleDeckViewportFit(frame);
      requestAnimationFrame(() => {
        if (frame.dataset.deckReady !== "1" || Number(ed.sel) !== target) return;
        frame.classList.remove("font-pending");
        frame.classList.add("font-ready");
      });
    }));
  };
  positionThenReveal();
  return true;
}

function bindDynamicDeckNavigation() {
  const frame = $("#canvas-deck");
  const deckKind = frame?.dataset.deckKind;
  const deckId = frame?.dataset.deckId;
  if (!frame || !deckKind || ed.kind !== deckKind || String(ed.id) !== deckId) return;
  try {
    const deckWindow = frame.contentWindow;
    const isBlankLoad = (() => {
      try { return deckWindow.location.href === "about:blank"; } catch { return false; }
    })();
    if (isBlankLoad) return;
    scheduleDeckViewportFit(frame);
    const sync = (byUser) => {
      if (ed.kind !== deckKind || String(ed.id) !== deckId || frame.contentWindow !== deckWindow) return;
      // The hidden player may finish loading after the user has switched back to
      // the 00 progress view. Its delayed slidechange must not steal that choice.
      if (frame.hidden || ed.sel === 0) return;
      if (deckKind === "dynamic" && (frame.dataset.pendingSlide || frame.dataset.deckReady !== "1")) return;
      const n = dynamicDeckActiveSlide(frame);
      if (!n || n > ed.total || n === ed.sel) return;
      if (byUser) setFollow(false);
      ed.sel = n;
      markSel();
      updateNav();
    };
    deckWindow.addEventListener("slidechange", () => sync(true));
    if (deckKind === "dynamic") {
      installDynamicDeckAdapter(frame);
      if (typeof deckWindow.__deckGo === "function") {
        const pending = Number(frame.dataset.pendingSlide) || ed.sel;
        if (pending > 0) revealDynamicDeckSlide(frame, pending);
      } else {
        // Legacy output without a callable navigator keeps URL-based paging as
        // a compatibility fallback.
        frame.dataset.deckReady = "fallback";
        delete frame.dataset.pendingSlide;
        frame.classList.remove("font-pending");
        frame.classList.add("font-ready");
        frame.hidden = false;
        $("#canvas-img").hidden = true;
        $("#canvas-empty").hidden = true;
      }
    } else if (deckKind === "static") {
      const isBlankLoad = (() => {
        try { return deckWindow.location.href === "about:blank"; } catch { return false; }
      })();
      if (isBlankLoad) return;
      const hasNativePlayer = !!deckWindow.cleanDeck?.go || typeof deckWindow.__deckGo === "function";
      if (hasNativePlayer) {
        if (editorPresentationKind() === "dynamic") stabilizeDynamicDeckPlayer(frame);
        else stabilizeStaticDeckPlayer(frame);
        revealStaticDeckWhenFontsReady(frame, deckWindow);
      } else {
        // Legacy/incomplete output without a player: preserve the rendered PNG fallback.
        keepStaticPng(ed.sel);
      }
    }
    setTimeout(() => scheduleDeckViewportFit(frame), 120);
    sync(false);
  } catch {
    // 非同源 Deck 无法读取内部状态；当前内置动态链路始终为同源地址。
  }
}

/* ----- AI 制作活动流：底层日志增量追加，界面只呈现可读动作 ----- */
function evKey(ev) {
  if (ev.event_id) return `e:${ev.event_id}`;
  if (ev.k === "tool" || ev.k === "tool_result") return `${ev.k}:${ev.tool_use_id || ""}:${ev.tool || ""}:${ev.hint || ""}`;
  if (ev.k === "provider" || ev.k === "status") return `${ev.k}:${ev.event_type || ev.phase || ev.status || ""}:${ev.seq || ""}`;
  return `${ev.k || "text"}:${ev.s || ""}`;
}
function livePreviewLabel(event, { thinking = false } = {}) {
  if (!event.partial) return thinking ? "思考已完成" : "";
  const interrupted = event.phase === "interrupted"
    || ["failed", "interrupted", "completed", "rejected", "canceled", "cancelled", "stopped", "paused_recoverable", "error"].includes(ed.status);
  return thinking ? (interrupted ? "思考已中断" : "思考中")
    : interrupted ? "已中断 · 非最终消息" : "生成中 · 非最终消息";
}
function liveAgentText(event) {
  // 新实时流已在写入时脱敏，保留模型原文，不把流式英文改写成固定进度文案。
  return event.event_id ? String(event.s || "").trim() : cleanAgentText(event.s);
}
function evLine(ev) {
  if (ev.k === "tool") return (TOOL_LABEL[ev.tool] || ev.tool) + " " + (ev.hint || "");
  if (ev.k === "tool_result") return `${TOOL_LABEL[ev.tool] || ev.tool} · ${ev.ok ? "完成" : "失败"}`;
  if (ev.k === "analysis") return `分析摘要 · ${ev.s || ""}`;
  if (ev.k === "provider") return `Provider ${ev.phase || "状态更新"}`;
  return ev.s || ev.status || "";
}
function agentKey(n) {
  return ed.pageAgents?.[String(Number(n))] || ("slide_" + pad2(n));
}

function agentPages(key) {
  return Object.entries(ed.pageAgents || {})
    .filter(([, owner]) => owner === key)
    .map(([page]) => Number(page))
    .filter(Number.isFinite)
    .sort((a, b) => a - b);
}

function pageAgentLabel(key, n) {
  if (key === "orch") return ed.pageAgents?.[String(Number(n))] === key
    ? "由主 Agent 直接负责本页制作与修订" : "整册统筹与页面复核";
  const pages = agentPages(key);
  if (pages.length > 1) {
    const related = pages.filter((page) => page !== Number(n));
    return related.length
      ? `本页与第 ${related.map((page) => Number(page)).join("、")} 页保持统一设计`
      : "正在统一制作这一组关联页面";
  }
  return "正在制作本页内容与视觉";
}

function evRow(ev) {
  const d = document.createElement("div");
  d.className = `lv-row${ev.k === "analysis" ? " analysis" : ""}${ev.k === "tool_result" && !ev.ok ? " failed" : ""}`;
  if (ev.k === "tool") {
    d.innerHTML = `<span class="lv-p lv-cmd">✦</span><span class="lv-fn">${escapeHtml(TOOL_LABEL[ev.tool] || "推进任务")}</span>` +
      `<span class="lv-hint">${escapeHtml(compactActivityHint(ev.hint || ""))}</span>`;
  } else if (ev.k === "tool_result") {
    d.innerHTML = `<span class="lv-p lv-cmd">${ev.ok ? "✓" : "!"}</span><span class="lv-fn">${escapeHtml(TOOL_LABEL[ev.tool] || ev.tool || "工具")}</span>` +
      `<span class="lv-hint">${escapeHtml(ev.ok ? `完成${ev.duration_ms ? ` · ${ev.duration_ms}ms` : ""}` : `失败 · ${ev.error_code || "执行错误"}`)}</span>`;
  } else if (ev.k === "analysis") {
    d.innerHTML = `<span class="lv-p">◇</span><span class="lv-text"><b>分析摘要</b> · ${escapeHtml(ev.s || "")}</span>`;
  } else if (ev.k === "provider" || ev.k === "status") {
    d.innerHTML = `<span class="lv-p">·</span><span class="lv-text">${escapeHtml(evLine(ev))}</span>`;
  } else {
    d.innerHTML = `<span class="lv-p">·</span><span class="lv-text">${escapeHtml(ev.s)}</span>`;
  }
  return d;
}

// 增量同步:只把"上次末行之后"的新事件 append 进终端,老行不动 → 不闪
function syncStream(body, evs, lines, rowFn = evRow, cap = 200, rowSel = ".lv-row") {
  if (!body) return;
  const keys = evs.map(evKey);
  let startIdx = 0;
  if (lines.length) {
    const idx = keys.lastIndexOf(lines[lines.length - 1]);
    if (idx >= 0) startIdx = idx + 1;
    else { body.querySelectorAll(rowSel).forEach((e) => e.remove()); lines.length = 0; }
  }
  if (startIdx >= keys.length) return;
  const caret = body.querySelector(".lv-caret");
  for (let i = startIdx; i < keys.length; i++) {
    body.insertBefore(rowFn(evs[i]), caret);    // caret 为 null 时等价 appendChild
    lines.push(keys[i]);
  }
  if (lines.length > cap) {
    const extra = lines.length - cap;
    lines.splice(0, extra);
    [...body.querySelectorAll(rowSel)].slice(0, extra).forEach((e) => e.remove());
  }
  body.scrollTo({ top: body.scrollHeight, behavior: "smooth" });
}

function compactActivityHint(value) {
  const clean = String(value || "").replace(/\\/g, "/").split("/").pop() || "";
  return clean.length > 34 ? `${clean.slice(0, 31)}…` : clean;
}

const TOOL_PROGRESS = {
  read: ["整理页面资料", "正在读取页面计划与参考素材"],
  read_file: ["整理页面资料", "正在读取页面计划与参考素材"],
  write: ["搭建页面内容", "正在编写本页内容与视觉结构"],
  write_file: ["搭建页面内容", "正在编写本页内容与视觉结构"],
  edit: ["调整页面细节", "正在优化页面布局与视觉细节"],
  edit_file: ["调整页面细节", "正在优化页面布局与视觉细节"],
  patch: ["调整页面细节", "正在根据检查结果优化布局与视觉细节"],
  inspect: ["视觉检查", "正在准备实际图片与检查反馈"],
  vision_analyze: ["检查画面质量", "正在检查版式、可读性与画面完整性"],
  image_generate: ["制作视觉素材", "正在生成本页需要的视觉素材"],
  fetch_image: ["下载图片", "正在将网络图片保存到本地"],
  web_search: ["搜索参考资料", "正在搜索内容与视觉参考"],
  web_fetch: ["阅读参考资料", "正在整理检索到的有效信息"],
  delegate_task: ["安排页面制作", "正在安排多页内容与视觉同步推进"],
  bash: ["渲染与检查页面", "正在渲染页面预览并检查结果"],
  terminal: ["渲染与检查页面", "正在运行页面制作与检查工具"],
};

function terminalProgress(hint = "") {
  const value = String(hint).toLowerCase();
  if (/\brender\b|deck\.py/.test(value)) return ["渲染页面预览", "正在渲染最新页面，准备进行视觉检查"];
  if (/validate|lint|check|test/.test(value)) return ["检查页面结构", "正在检查页面结构与输出完整性"];
  if (/grep|sed|head|find|\bls\b/.test(value)) return ["核对页面资源", "正在核对页面结构、素材与样式定义"];
  return TOOL_PROGRESS.terminal;
}

function toolProgress(ev) {
  if (ev.tool === "terminal" || ev.tool === "bash") return terminalProgress(ev.hint);
  if (ev.tool === "dynamic_action") {
    const value = String(ev.hint || "").toLowerCase();
    const label = /检索|搜索|search|fetch/.test(value) ? "查找并整理参考资料"
      : /配图|图像|image/.test(value) ? "准备页面视觉素材"
        : /检查|复审|vision|review|validate/.test(value) ? "检查页面呈现效果"
          : /渲染|render/.test(value) ? "生成最新页面预览"
            : /撰写|修改|编写|write|edit|patch/.test(value) ? "完善页面内容与画面"
              : "推进本页制作";
    return [label, "正在执行当前制作步骤，完成后会自动继续下一项。"];
  }
  return TOOL_PROGRESS[ev.tool] || [TOOL_LABEL[ev.tool] || "推进页面制作", "正在推进当前页面的制作"];
}

function friendlyToolHint(ev) {
  const value = String(ev.hint || "").replace(/\\/g, "/");
  if (!value || /[{}\"]/g.test(value)) return "";
  const match = value.match(/(?:^|\/)([^/]+\.(?:html?|png|jpe?g|svg|md|css|json))\b/i);
  return match ? match[1] : "";
}

function cleanAgentText(value) {
  return String(value || "")
    .replace(/^\[\d+\]\s*💬\s*/, "")
    .replace(/^💬\s*/, "")
    .replace(/\r\n?/g, "\n")
    .replace(/[^\S\n]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function agentStoryHtml(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

// A compact, dependency-free Markdown renderer for Agent narrative. Raw HTML
// is escaped before formatting, so model output cannot inject DOM/script. The
// supported subset intentionally matches what Agents emit most often:
// headings, paragraphs, lists, checklists, quotes and fenced code blocks.
function agentMarkdownHtml(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let paragraph = [];
  let listType = "";
  let listItems = [];
  let quoteLines = [];
  let codeLines = [];
  let inCode = false;
  let codeLanguage = "";
  let tableSkipUntil = -1;

  const inline = (text) => escapeHtml(String(text || ""))
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  const flushParagraph = () => {
    if (!paragraph.length) return;
    output.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    output.push(`<${listType}>${listItems.map((item) => `<li${item.checked == null ? "" : ` class=\"task-item ${item.checked ? "checked" : ""}\"`}>
      ${item.checked == null ? "" : `<span class=\"task-check\" aria-hidden=\"true\">${item.checked ? "✓" : ""}</span>`}${inline(item.text)}</li>`).join("")}</${listType}>`);
    listType = "";
    listItems = [];
  };
  const flushQuote = () => {
    if (!quoteLines.length) return;
    output.push(`<blockquote>${quoteLines.map(inline).join("<br>")}</blockquote>`);
    quoteLines = [];
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };
  const tableCells = (line) => String(line || "")
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((cell) => cell.trim());
  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  };

  lines.forEach((rawLine, lineIndex) => {
    if (lineIndex <= tableSkipUntil) return;
    const line = rawLine.trim();
    const fence = line.match(/^```\s*([\w+-]*)/);
    if (fence) {
      if (inCode) {
        output.push(`<pre><code${codeLanguage ? ` data-language=\"${escapeHtml(codeLanguage)}\"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        codeLanguage = "";
        inCode = false;
      } else {
        flushAll();
        inCode = true;
        codeLanguage = fence[1] || "";
      }
      return;
    }
    if (inCode) {
      codeLines.push(rawLine);
      return;
    }
    if (!line) {
      flushAll();
      return;
    }
    const nextLine = lines[lineIndex + 1]?.trim() || "";
    if (line.includes("|") && nextLine.includes("|") && isTableDivider(nextLine)) {
      flushAll();
      const headers = tableCells(line);
      const rows = [];
      let cursor = lineIndex + 2;
      while (cursor < lines.length) {
        const row = lines[cursor].trim();
        if (!row || !row.includes("|") || /^```/.test(row)) break;
        const cells = tableCells(row);
        if (cells.length < 2) break;
        rows.push(cells);
        cursor += 1;
      }
      tableSkipUntil = cursor - 1;
      const columnCount = Math.max(headers.length, ...rows.map((row) => row.length));
      const normalizedHeaders = Array.from({ length: columnCount }, (_, index) => headers[index] || "");
      output.push(`<div class="agent-table-wrap"><table><thead><tr>${normalizedHeaders.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${Array.from({ length: columnCount }, (_, index) => `<td>${inline(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      return;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushAll();
      const level = Math.min(4, heading[1].length + 1);
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      return;
    }
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushAll();
      output.push("<hr>");
      return;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph(); flushList();
      quoteLines.push(quote[1]);
      return;
    }
    const unordered = line.match(/^[-*+]\s+(.+)$/);
    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph(); flushQuote();
      const nextType = ordered ? "ol" : "ul";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      const itemText = (unordered || ordered)[1];
      const task = itemText.match(/^\[([ xX])\]\s*(.*)$/);
      listItems.push(task
        ? { text: task[2], checked: task[1].toLowerCase() === "x" }
        : { text: itemText, checked: null });
      return;
    }
    flushList(); flushQuote();
    paragraph.push(line);
  });
  if (inCode) output.push(`<pre><code${codeLanguage ? ` data-language=\"${escapeHtml(codeLanguage)}\"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  flushAll();
  return output.join("");
}

function runtimeEventState(event) {
  if (!event) return null;
  switch (event.event_type) {
    case "attachment_parse_queued":
    case "attachment_parse_started":
    case "attachment_parse_finished": return [event.s, `${event.attachment_id || "附件"} · ${attachmentParseLabel(event)}；后台解析状态不代表 Research 已阅读或语义核验。`];
    case "agent_started": return ["Agent 已启动", "正在准备本轮运行上下文；模型是否开始请求以 Provider 事件为准。"];
    case "agent_completed": return ["Agent 已完成", "该角色本轮执行已结束。"];
    case "deck_completion_checked": return [event.status === "ready" ? "制作完成校验通过" : "制作完成校验未通过",
      "这是 Harness 自动完成校验，不是模型工具调用，也不代表离线内容 QC 或 clean 准入。"];
    case "agent_failed": return ["Agent 执行异常", event.error_summary || event.error_type || "请查看该角色的 Trace。"];
    case "agent_context_compacted": {
      const before = Number(event.tokens_before || event.estimated_tokens || 0);
      const after = Number(event.tokens_after || 0);
      const budget = Number(event.input_budget || 0);
      const usage = [before ? `触发前估算 ${before.toLocaleString()} tokens` : "",
        after ? `压缩后估算 ${after.toLocaleString()} tokens` : "",
        budget ? `输入预算 ${budget.toLocaleString()} tokens` : ""].filter(Boolean).join(" · ");
      return ["上下文压缩完成", `${usage || "旧历史已压缩为续接摘要"}；任务会从新上下文段继续。`];
    }
    case "agent_context_compaction_failed": return ["上下文压缩未完成",
      `${event.error_summary || event.error_type || "压缩摘要没有形成可用结果。"}${event.recoverable ? " 已保留检查点，可恢复或缩小任务后续接。" : ""}`];
    case "resource_wait_started": return ["等待模型资源", "正在等待本地资源槽，尚未发送本轮 Provider 请求。"];
    case "resource_wait_failed": return ["本地资源等待失败", "本轮尚未请求 Provider；请查看资源等待记录。"];
    case "provider_started": {
      const kind = event.request_kind_label || "模型请求";
      const budget = event.actual_max_tokens ? ` · 本轮输出上限 ${event.actual_max_tokens}` : "";
      if (event.request_kind === "context_compaction") return ["正在压缩上下文",
        `${event.model || "模型"} 已开始生成内部续接摘要${budget}；这是运行时维护步骤，不是任务正文。`];
      return [`${kind} · 等待模型响应`, `${event.model || "模型"} 已开始请求${budget}。${event.request_kind_note ? event.request_kind_note + " " : ""}${event.streaming === false ? "本次未开启流式，完整响应返回后显示文本或工具动作。" : event.streaming === true ? "本次已开启流式，收到可展示内容后实时更新。" : "收到响应后将展示文本或工具动作。"}`];
    }
    case "provider_succeeded": return [`${event.request_kind_label || "模型请求"} · 已响应`, "响应已返回，正在处理文本或工具调用；没有自然语言消息不代表没有响应。"];
    case "provider_failed": return [`${event.request_kind_label || "模型请求"} · 请求失败`, event.error_summary || event.error_kind || "请查看 Provider 记录。"];
    case "provider_failover": return ["Provider 通道切换", `正在切换到 ${event.channel_id || "备用通道"}。`];
    case "language_drift": return ["工作语言疑似漂移", event.detail || "当前工作语言要求与可见模型正文不一致，已记录用于排查。"];
    default: return null;
  }
}

function agentCurrentState(evs, { running, rendered, n }) {
  if (rendered) return running
    ? ["本页已有最新预览", "页面已生成预览，仍可能根据整套演示的检查结果继续优化。"]
    : ["本页制作已完成", "页面已经过渲染与检查，可以切换到页面预览查看结果。"];
  const latest = [...evs].reverse().find((ev) => ev.k !== "context"
    && (runtimeEventState(ev) || ["tool", "tool_result"].includes(ev.k) || cleanAgentText(ev.s)));
  if (!latest) {
    return running
      ? ["等待 Agent 启动记录", n ? `第 ${n} 页尚无角色启动记录，不代表模型已经开始制作。` : "任务正在准备或等待调度，尚无模型请求记录。"]
      : ["暂无制作记录", "开始生成后，这里会展示当前正在进行的工作。"];
  }
  const runtimeState = runtimeEventState(latest);
  if (runtimeState) return runtimeState;
  if (latest.k === "tool") return toolProgress(latest);
  if (latest.k === "tool_result") {
    const label = TOOL_LABEL[latest.tool] || latest.tool || "当前步骤";
    return latest.ok
      ? [`${label}已完成`, latest.duration_ms ? `工具结果已返回，用时 ${latest.duration_ms} ms。` : "工具结果已返回，正在继续后续步骤。"]
      : [`${label}未通过`, `错误状态：${latest.error_code || "执行失败"}，Agent 将根据结果调整。`];
  }
  const text = cleanAgentText(latest.s);
  if (latest.partial) return [livePreviewLabel(latest), text];
  return [text.length > 54 ? `${text.slice(0, 53)}…` : text, latest.k === "analysis" ? "这是 Provider 明确返回的可展示分析摘要。" : "本轮判断已经完成，正在继续推进后续步骤。"];
}

function summarizeToolEvents(evs) {
  const groups = new Map();
  evs.forEach((ev, index) => {
    if (ev.k !== "tool") return;
    const [label] = toolProgress(ev);
    const previous = groups.get(label) || { label, count: 0, hint: "", last: -1 };
    previous.count += 1;
    previous.last = index;
    previous.hint = friendlyToolHint(ev) || previous.hint;
    groups.set(label, previous);
  });
  return [...groups.values()].sort((a, b) => b.last - a.last).slice(0, 7);
}

function compactTraceValue(value, maxLength = 220) {
  if (value == null || value === "") return "";
  let text;
  try { text = typeof value === "string" ? value : JSON.stringify(value); }
  catch { text = String(value); }
  text = text.replace(/\s+/g, " ").trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function liveToolTransactions(evs, { limit = 60 } = {}) {
  const transactions = new Map();
  (evs || []).forEach((event, index) => {
    if (!["tool", "tool_result"].includes(event.k) || !event.tool_use_id) return;
    const id = String(event.tool_use_id);
    const current = transactions.get(id) || {
      id, tool: event.tool || "tool", use: null, result: null,
      order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index,
      agent: event.agent || "",
    };
    current.tool = event.tool || current.tool;
    if (event.k === "tool") current.use = event;
    else current.result = event;
    current.order = Math.min(current.order, Number.isFinite(Number(event.seq)) ? Number(event.seq) : index);
    current.agent ||= event.agent || "";
    transactions.set(id, current);
  });
  return [...transactions.values()].sort((a, b) => b.order - a.order).slice(0, limit);
}

function liveReadFileSummary(transaction, active) {
  if (!["read_file", "read"].includes(transaction.tool)) return null;
  const args = transaction.use?.args || {};
  const result = transaction.result;
  let receipt = result?.result;
  if (typeof receipt === "string") {
    try { receipt = JSON.parse(receipt); } catch { receipt = null; }
  }
  const path = String(args.path || receipt?.path || "");
  const file = path.split("/").pop() || "文件名未提供";
  let progress = !result ? active ? "读取中 · 等待回执" : "读取中断 · 范围未确认"
    : !result.ok ? "读取失败 · 范围未确认" : "已返回 · 读取范围未提供";
  if (result?.ok && receipt && typeof receipt === "object") {
    const { line_count: total, returned_line_count: count, offset, next_offset: next } = receipt;
    const countsKnown = Number.isInteger(total) && total >= 0 && Number.isInteger(count) && count >= 0;
    if (String(args.query || "").trim() || receipt.mode === "search_snippets") {
      progress = `搜索片段 · ${countsKnown ? `返回 ${count} / ${total} 行 · ` : ""}不代表全文`;
    } else if (countsKnown && receipt.mode === "line_range" && Number.isInteger(offset) && offset >= 1) {
      if (offset === 1 && count === total && next === null && !receipt.truncated) {
        progress = total === 0 ? "全文已返回 · 空文件（0 行）" : `全文已返回 · ${count} / ${total} 行`;
      } else {
        progress = count > 0 ? `本次部分读取 · 第 ${offset}–${offset + count - 1} 行 / 共 ${total} 行`
          : `未返回正文 · 文件共 ${total} 行`;
        if (Number.isInteger(next) && next >= 1) progress += ` · 可从第 ${next} 行继续`;
      }
    }
  }
  return { path, file, progress };
}

function liveTerminalLabel(command) {
  if (typeof command !== "string" || !command.trim()) return "运行制作工具 · 命令未记录";
  const words = [];
  let rest = command.trimStart();
  // 只展示受限命令的执行器、脚本和动作；不从备注猜用途，也不在前端执行 shell。
  while (rest && words.length < 32) {
    const token = rest.match(/^(?:"([^"\\]*)"|'([^']*)'|([^\s"'\\]+))(?:\s+|$)/);
    if (!token) return "运行制作工具 · 命令见详情";
    words.push(token[1] ?? token[2] ?? token[3]);
    rest = rest.slice(token[0].length);
  }
  if (rest) return "运行制作工具 · 命令见详情";
  if (words[0] === "ls") {
    let options = true;
    const directories = [];
    words.slice(1).forEach(word => {
      if (options && word === "--") {
        options = false;
      } else if (options && word.startsWith("-")) {
        // ls 选项不属于查询目标；-- 后以连字符开头的目录仍照常展示。
      } else {
        directories.push(word);
      }
    });
    return `查看目录 · ${compactTraceValue((directories.length ? directories : ["."]).join(" · "), 100)}`;
  }
  const [interpreter, path, action] = words;
  if (!/^python(?:\d+(?:\.\d+)*)?$/.test(interpreter?.split("/").pop()) || !path?.endsWith(".py") || !action) {
    return "运行制作工具 · 命令见详情";
  }
  return `运行制作工具 · ${compactTraceValue(`${path.split("/").pop()} ${action}`, 100)}`;
}

function workflowDelegationNodes(transaction) {
  if (transaction.tool !== "delegate_submit" || transaction.result?.ok === false) return [];
  let receipt = transaction.result?.result;
  if (typeof receipt === "string") {
    try { receipt = JSON.parse(receipt); } catch { receipt = null; }
  }
  const recorded = Array.isArray(receipt?.tasks) ? receipt.tasks : [];
  const requested = Array.isArray(transaction.use?.args?.tasks) ? transaction.use.args.tasks : recorded;
  const ids = [...new Set(requested.map(task => String(task?.task_id || "")).filter(Boolean))];
  // 回执可填补实时状态尚未到达的窗口；请求参数本身不证明任务已被接受。
  const tasks = new Map([...recorded, ...(ed.muralRuntime?.tasks || [])]
    .filter(task => task?.task_id).map(task => [String(task.task_id), task]));
  const nodes = new Map();
  workflowTaskGraph([...tasks.values()], ed.agentTimings || {}, ed.feed || {}, ed.status)
    .nodes.forEach(node => [...new Set([node.taskId, ...(node.taskIds || [])])]
      .filter(Boolean).forEach(id => nodes.set(String(id), node)));
  return ids.map(id => {
    const node = nodes.get(id);
    const currentId = ed.agentTimings?.[node?.agentKey]?.task_id;
    const ownerId = tasks.get(id)?.owner_id;
    // 续接仍展示原 Owner 的当前任务，不能按重叠页码误连到其他 Agent。
    return ownerId && currentId && tasks.get(String(currentId))?.owner_id === ownerId
      ? nodes.get(String(currentId)) || node : node;
  }).filter(Boolean);
}

function workflowDelegationMarkup(transaction) {
  const nodes = workflowDelegationNodes(transaction);
  if (!nodes.length) return "";
  return `<section class="workflow-delegation-cards" aria-label="本次委派的 Agent · 实时状态" data-delegation-call="${escapeHtml(transaction.id)}">
    ${nodes.map(node => `<article class="orch-entry specialist role-${escapeHtml(node.role)}" data-embedded-agent="true" data-owner-node-id="${escapeHtml(node.id)}" data-agent-key="${escapeHtml(node.agentKey)}" data-workflow-task-id="${escapeHtml(node.taskId)}" tabindex="0" role="link" aria-label="查看 ${escapeHtml(node.title)} 的迭代">${orchestrationEntryContent(workflowOwnerProgressEntry(node))}</article>`).join("")}
  </section>`;
}

function workflowOwnerProgressEntry(node, previous = null) {
  const events = (ed.feed?.[node.agentKey] || []).filter(event => !event.partial && event.k !== "context"
    && (event.task_id ? event.task_id === node.taskId : !node.segment || event.segment_id === node.segment));
  const tasks = ed.muralRuntime?.tasks || [];
  const ownerId = tasks.find(task => task.task_id === node.taskId)?.owner_id;
  const assigned = ownerId ? tasks.filter(task => task.owner_id === ownerId).flatMap(task => task.pages || []) : [];
  const pages = [...new Set([...agentPages(node.agentKey), ...assigned, ...node.pages])].sort((a, b) => a - b);
  const meta = {...orchestrationAgentMeta(node.agentKey || node.role), role: node.role, pages};
  if (node.role === "slide") {
    meta.label = `${pages.length > 1 ? "Group Owner" : "Slide Owner"}${pages.length ? ` · P${pages.join(" / P")}` : ""}`;
    meta.note = pages.length > 1 ? "同一 Owner 连续制作与联合复核" : "页面制作与像素检查";
  }
  const artifacts = ed.specialistArtifacts?.agents?.[node.agentKey] || {};
  const entry = node.role === "slide" ? {
    type: "slide", meta, steps: slideProgressSteps(meta, events),
    resultDetail: [...events].reverse().find(event => event.k === "text")?.s || "",
  } : specialistProgressEntry(meta, events, artifacts);
  const segment = node.segment || [...events].reverse().find(event => event.task_id === node.taskId && event.segment_id)?.segment_id
    || (previous?.taskId === node.taskId ? previous.segment : "");
  return {...entry, ownerNode: node, taskId: node.taskId, segment, repairs: Number(node.repairs) || 0,
    key: `owner-progress:${node.agentKey || node.taskId}`, order: previous?.order ?? events[0]?.seq ?? 0,
    artifactRels: previous?.artifactRels || workflowArtifactRelsFromEvents(events, meta),
    completed: node.status === "complete",
    failed: ["failed", "start_failed", "dependency_failed"].includes(node.status),
    timing: agentTimingFor(node.agentKey)};
}

function slideProgressSteps(meta, events) {
  const step = (label, tools) => specialistToolStep(label, events, tools);
  return [step("读取制作依据", ["read_file", "read"]),
    step("制作与修改页面", ["write_file", "patch", "edit_file"]),
    step("视觉检查记录（单页 / 联系表 / 素材）", ["inspect", "inspect_group", "vision_analyze"])];
}

function workflowDelegationStateKey() {
  return JSON.stringify([ed.muralRuntime?.tasks || [], ed.agentTimings || {},
    Object.entries(ed.feed || {}).map(([agent, events]) => [agent, events.length, events.at(-1)]),
    [...(ed.rendered || [])], ed.rtimes || {}, ed.specialistArtifacts || {}]);
}

function liveToolTransactionMarkup(transaction, { showAgent = false, open = false, compact = false, active = true } = {}) {
  const result = transaction.result;
  const failed = !!result && !result.ok;
  const running = !result && active;
  const args = compactTraceValue(transaction.use?.args, 260);
  const output = compactTraceValue(result?.result, 260);
  const argsDetail = transaction.use?.args && Object.keys(transaction.use.args).length
    ? workflowTraceSafeJson(transaction.use.args) : "";
  const outputDetail = result && result.result !== undefined
    ? workflowTraceSafeJson(result.result) : "";
  const duration = result?.duration_ms ? `${Number(result.duration_ms).toLocaleString()} ms` : "";
  const status = !result && !active ? "未收到结束回执" : running ? "运行中" : failed
    ? `失败${result.error_code ? ` · ${result.error_code}` : ""}`
    : `完成${duration ? ` · ${duration}` : ""}`;
  const agent = showAgent && transaction.agent
    ? `${orchestrationAgentMeta(transaction.agent).label} · ` : "";
  const read = liveReadFileSummary(transaction, active);
  const editPath = ["patch", "write_file"].includes(transaction.tool)
    ? String(transaction.use?.args?.path || result?.result?.path || "") : null;
  const filePath = read?.path ?? editPath;
  const toolLabel = TOOL_LABEL[transaction.tool] || transaction.tool;
  const orch = ["orch", "orchestrator"].includes(transaction.agent);
  const delegatedTasks = transaction.tool === "delegate_submit" && Array.isArray(transaction.use?.args?.tasks)
    ? transaction.use.args.tasks : [];
  const isFailedTaskStatus = task =>
    ["failed", "dependency_failed", "start_failed", "skipped"].includes(String(task?.status || "").toLowerCase());
  const invalidDelegationArguments = transaction.tool === "delegate_submit" && failed
    && ["invalid_arguments", "missing_arguments", "unknown_arguments"].includes(result.error_code);
  const delegatedRoles = new Map();
  delegatedTasks.forEach(task => {
    const role = String(task?.role || "").trim().toLowerCase();
    if (role) delegatedRoles.set(role, (delegatedRoles.get(role) || 0) + 1);
  });
  const roleNames = {research: "Research", image: "Image", slide: "Slide"};
  const delegationRoles = [...delegatedRoles].map(([role, count]) =>
    `${roleNames[role] || role}${count > 1 ? ` × ${count}` : ""}`).join(" · ") || "任务角色未确认";
  const runtimeTasks = typeof ed !== "undefined" && Array.isArray(ed.muralRuntime?.tasks)
    ? ed.muralRuntime.tasks : [];
  const delegatedTaskIds = new Set(delegatedTasks.map(task => String(task?.task_id || "")).filter(Boolean));
  const delegatedRuntimeTasks = result?.ok
    ? runtimeTasks.filter(task => delegatedTaskIds.has(String(task?.task_id || ""))) : [];
  const failedDelegatedTask = delegatedRuntimeTasks.find(isFailedTaskStatus);
  const waitPayload = transaction.tool === "delegate_wait" && result?.ok
    && result.result && typeof result.result === "object" ? result.result : null;
  const waitFailedTask = Array.isArray(waitPayload?.tasks)
    ? waitPayload.tasks.find(isFailedTaskStatus) : null;
  const waitFailureEvent = Array.isArray(waitPayload?.events)
    ? waitPayload.events.find(event => ["task_failed", "task_start_failed", "dependency_failed"]
      .includes(String(event?.type || event?.event_type || "").toLowerCase())) : null;
  const waitUnresolvedFailure = Array.isArray(waitPayload?.unresolved_failures)
    ? waitPayload.unresolved_failures[0] : null;
  const childFailure = failedDelegatedTask || waitFailedTask || waitFailureEvent || waitUnresolvedFailure || null;
  const childFailureText = String(childFailure?.error || childFailure?.detail || childFailure?.message
    || childFailure?.status || childFailure?.type || childFailure?.event_type || "").trim();
  const childFailureCode = childFailureText.match(/\b([a-z][a-z0-9_]+):/)?.[1]
    || String(childFailure?.error_code || childFailure?.code || "").trim();
  const delegatedStatuses = delegatedRuntimeTasks.map(task => String(task?.status || "").toLowerCase());
  const delegationStatus = failedDelegatedTask
    ? `提交完成；子任务后续失败${childFailureCode ? ` · ${childFailureCode}` : ""}`
    : delegatedRuntimeTasks.length && delegatedStatuses.every(value => value === "succeeded") ? "同事已完成"
    : delegatedStatuses.some(value => ["running", "processing"].includes(value)) ? "同事执行中"
    : delegatedRuntimeTasks.length ? "已提交 · 等待执行" : "提交已处理";
  const pendingRequestCount = Array.isArray(waitPayload?.pending_requests) ? waitPayload.pending_requests.length : 0;
  const waitStatus = childFailure && transaction.tool === "delegate_wait"
    ? `等待完成；收到子任务失败事件${childFailureCode ? ` · ${childFailureCode}` : ""}`
    : pendingRequestCount ? `等待完成；收到协助请求 × ${pendingRequestCount}`
    : waitPayload?.all_terminal ? "等待完成；当前批次已结束" : "等待完成";
  const displayFailed = failed;
  const displayStatus = transaction.tool === "delegate_submit" && result && !failed
    ? `${delegationStatus}${duration ? ` · ${duration}` : ""}`
    : transaction.tool === "delegate_wait" && result && !failed
      ? `${waitStatus}${duration ? ` · ${duration}` : ""}` : status;
  const scope = transaction.use?.args?.scope;
  const unifiedVisual = transaction.tool === "inspect";
  const target = transaction.use?.args || {};
  const visualKind = Array.isArray(target.page) && target.page.length > 1 ? "多页联系表检查"
    : target.page !== undefined ? "单页视觉检查" : "图片检查";
  const visual = orch && (unifiedVisual || (transaction.tool === "vision_analyze" && ["deck", "page"].includes(scope)));
  const label = visual ? `Orchestrator · ${unifiedVisual ? visualKind : scope === "deck" ? "整册视觉复核" : "页面视觉复核"}`
    : orch && transaction.tool === "verification_submit" && transaction.use?.args?.status === "issues_found" ? "Orchestrator · 提交复核问题"
    : invalidDelegationArguments ? "委派参数格式错误，任务未启动"
    : orch && transaction.tool === "delegate_submit" ? `拉起同事 · ${delegationRoles}`
    : orch && transaction.tool === "delegate_wait" ? "等待后台任务状态"
    : read ? `读取文件 · ${read.file}` : editPath !== null
    ? `${toolLabel} · ${editPath.split("/").pop() || "文件名未记录"}`
    : transaction.tool === "terminal" ? liveTerminalLabel(transaction.use?.args?.command) : toolLabel;
  return `<details class="agent-action live-transaction ${running ? "active running" : displayFailed ? "failed" : result ? "succeeded" : "interrupted"}${compact ? " compact-tool" : ""}" data-detail-key="tool-${escapeHtml(transaction.id)}" data-tool-use-id="${escapeHtml(transaction.id)}"${open ? " open" : ""}>
    <summary><span>${running ? "•" : displayFailed ? "!" : result ? "✓" : "○"}</span>
      <div class="live-transaction-copy"><b${filePath !== null ? ` class="live-tool-file" title="${escapeHtml(filePath)}"` : ""}>${escapeHtml(`${agent}${label}`)}</b>
        ${read ? `<small class="live-read-progress" title="仅说明本次工具返回的范围，不代表模型已理解或落实文件要求。">${escapeHtml(read.progress)}</small>` : ""}
        ${visual ? `<small class="live-read-progress">${escapeHtml(target.path || (target.page !== undefined ? `页码 ${[].concat(target.page).join(", ")}` : "图片路径未记录"))} · ${unifiedVisual ? "实际图片与检查反馈见回执；执行成功不等于质量通过，只读复核不取得修改权" : "原生看图，判断见本 Agent 后续消息；不是独立 Critic"}</small>` : ""}
        ${!compact && args ? `<i><strong>参数</strong>${escapeHtml(args)}</i>` : ""}
        ${!compact && output ? `<i><strong>结果</strong>${escapeHtml(output)}</i>` : ""}
      </div><em>${escapeHtml(displayStatus)}</em><i class="live-transaction-chevron" aria-hidden="true"></i>
    </summary>
    <div class="live-transaction-detail">
      ${argsDetail || transaction.use?.details_available ? `<section${transaction.use?.details_available ? ` data-live-event-seq="${Number(transaction.use.seq)}" data-live-detail-field="args"` : ""}><b>调用参数</b><pre>${escapeHtml(argsDetail || "展开后读取完整参数…")}</pre></section>` : ""}
      ${outputDetail || result?.details_available ? `<section${result?.details_available ? ` data-live-event-seq="${Number(result.seq)}" data-live-detail-field="result"` : ""}><b>执行结果</b><pre>${escapeHtml(outputDetail || "展开后读取完整结果…")}</pre></section>` : !result ? `<p>${running ? "工具仍在执行，结果返回后会显示在这里。" : "本次运行已结束或暂停；此工具没有结束回执，不能判为成功。"}</p>` : ""}
      ${childFailure && !failed ? `<p class="live-transaction-error">子任务后续失败（非本工具失败）：${escapeHtml(childFailureText || "未记录具体原因")}</p>` : ""}
      ${failed && result.error_code ? `<p class="live-transaction-error">错误代码：${escapeHtml(result.error_code)}</p>` : ""}
    </div>
  </details>`;
}

async function loadLiveToolDetails(details) {
  const deckId = ed.id;
  const cache = ed.liveToolDetails ||= new Map();
  await Promise.all([...details.querySelectorAll("[data-live-event-seq]")].map(async section => {
    const sequence = Number(section.dataset.liveEventSeq);
    if (!sequence || section.dataset.loaded === "1") return;
    const pre = section.querySelector("pre");
    pre.textContent = "正在读取完整内容…";
    if (!cache.has(sequence)) {
      const suffix = `/live-events/${sequence}`;
      cache.set(sequence, jget(trajectoryMode ? trajectoryDeckApi(deckId, suffix)
        : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${deckId}${suffix}`));
    }
    try {
      const event = await cache.get(sequence);
      if (ed.id !== deckId || ed.liveToolDetails !== cache || !section.isConnected) return;
      pre.textContent = workflowTraceSafeJson(event[section.dataset.liveDetailField]);
      section.dataset.loaded = "1";
    } catch {
      cache.delete(sequence);
      if (section.isConnected) pre.textContent = "完整内容读取失败，收起后展开可重试。";
    }
  }));
}

function reconcileAgentStories(list, stories) {
  const existing = new Map($$(".agent-message", list).map((card) => [card.dataset.storyKey, card]));
  if (!stories.length) {
    existing.forEach((card) => card.remove());
    if (!list.querySelector(".agent-story-empty")) {
      list.innerHTML = '<div class="agent-story-empty">正在整理上下文，关键判断会显示在这里。</div>';
    }
    return;
  }
  list.querySelector(".agent-story-empty")?.remove();
  const occurrences = new Map();
  const desired = stories.map((text, index) => {
    const occurrence = (occurrences.get(text) || 0) + 1;
    occurrences.set(text, occurrence);
    const key = JSON.stringify([text, occurrence]);
    let card = existing.get(key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.storyKey = key;
      card.innerHTML = `<span class="agent-avatar">${agentIdentityGlyph(editorPresentationKind())}</span><div><b>MURAL Presenter</b><p>${agentStoryHtml(text)}</p></div>`;
    }
    card.className = `agent-message${index === stories.length - 1 ? " latest" : ""}`;
    existing.delete(key);
    return card;
  });
  existing.forEach((card) => card.remove());
  desired.forEach((card, index) => {
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
}

function progressHistoryItems(evs) {
  const items = evs.flatMap((ev) => {
    if (ev.k === "context") return [];
    if (ev.k === "tool") {
      const [title, detail] = toolProgress(ev);
      return [{ type: "tool", title, text: detail, count: 1 }];
    }
    if (ev.k === "analysis") {
      const value = cleanAgentText(ev.s);
      return value ? [{ type: "analysis", title: "分析摘要", text: value, count: 1 }] : [];
    }
    const value = cleanAgentText(ev.s);
    return value ? [{ type: "message", title: "MURAL Presenter", text: value, count: 1 }] : [];
  });
  const toolIndexes = new Map();
  return items.reduce((result, item) => {
    if (item.type !== "tool") {
      result.push(item);
      return result;
    }
    const key = JSON.stringify([item.title, item.text]);
    const existingIndex = toolIndexes.get(key);
    if (existingIndex != null) result[existingIndex].count += 1;
    else {
      toolIndexes.set(key, result.length);
      result.push(item);
    }
    return result;
  }, []);
}

function pageAgentDisplayName(source, n) {
  if (source === "orch" && ed.pageAgents?.[String(Number(n))] === source) return "主 Agent 制作";
  if (String(source).startsWith("slide_group_")) return agentPages(source).length > 1
    ? "页面组设计 Agent" : `第 ${pad2(n)} 页设计 Agent`;
  if (/^slide_\d+/.test(String(source))) return `第 ${pad2(n)} 页设计 Agent`;
  if (String(source).startsWith("dynamic:")) return "动态创作 Agent";
  return "MURAL Presenter";
}

function completedMilestoneDetail(label, count, hints) {
  const amount = Math.max(1, Number(count) || 1);
  const files = [...new Set(hints || [])].slice(0, 3);
  const fileText = files.length ? `涉及 ${files.join("、")}。` : "";
  if (label === "整理页面资料") return `已读取 ${amount} 项页面规划、视觉规范与参考资料，明确本页内容和版式约束。${fileText}`;
  if (label === "搭建页面内容") return `已完成页面内容与视觉结构的初步搭建，共写入 ${amount} 次。${fileText}`;
  if (label === "调整页面细节") return `根据页面检查结果完成 ${amount} 次布局、样式或内容调整。${fileText}`;
  if (label === "渲染页面预览") return `已生成 ${amount} 版页面预览，用于逐轮核对画面效果。`;
  if (label === "检查页面结构") return `已完成 ${amount} 次页面结构与交付完整性检查。`;
  if (label === "核对页面资源") return `已完成 ${amount} 次页面结构、素材与样式核对。${fileText}`;
  if (label === "检查画面质量") return `已完成 ${amount} 次真实视觉检查，核对排版、可读性、遮挡、溢出与整体呈现。`;
  if (label === "制作视觉素材") return `已完成 ${amount} 次页面视觉素材生成与整理。${fileText}`;
  if (label === "搜索参考资料" || label === "阅读参考资料") return `已完成 ${amount} 次资料检索与内容核验，为本页文案和视觉提供依据。`;
  return `已完成“${label}”${amount > 1 ? `等 ${amount} 次相关操作` : ""}。${fileText}`;
}

function pageAgentProgressItems(evs, source, n) {
  const owner = pageAgentDisplayName(source, n);
  const groups = new Map();
  const messages = [];
  (evs || []).forEach((ev, index) => {
    const sequence = ev.stream_start_seq ?? ev.seq;
    const order = Number.isFinite(Number(sequence)) ? Number(sequence) : index;
    if (ev.k === "text" || ev.k === "analysis") {
      const text = liveAgentText(ev);
      if (text && !messages.some((item) => item.text === text)) {
        messages.push({
          type: ev.k === "analysis" ? "analysis" : "message",
          title: (ev.k === "analysis" ? `${owner} · 分析摘要` : owner) + (ev.partial ? ` · ${livePreviewLabel(ev)}` : ""),
          streamId: ev.stream_id || "",
          text, count: 1, order,
        });
      }
      return;
    }
    if (ev.k !== "tool") return;
    const [label] = toolProgress(ev);
    const group = groups.get(label) || { label, count: 0, hints: [], order };
    group.count += 1;
    const hint = friendlyToolHint(ev);
    if (hint && !group.hints.includes(hint)) group.hints.push(hint);
    groups.set(label, group);
  });
  const milestones = [...groups.values()].map((group) => ({
    type: "tool",
    title: `${owner} · ${group.label}`,
    text: completedMilestoneDetail(group.label, group.count, group.hints),
    count: group.count,
    order: group.order,
  }));
  const combined = [...milestones, ...messages].sort((a, b) => a.order - b.order);
  // Keep the page readable while retaining substantially more context than the
  // previous four-text cap. The final Agent result is always preserved.
  if (combined.length <= 12) return combined;
  const finalMessage = [...combined].reverse().find((item) => item.type === "message");
  const visible = combined.slice(-12);
  if (finalMessage && !visible.includes(finalMessage)) visible[visible.length - 1] = finalMessage;
  return visible.sort((a, b) => a.order - b.order);
}

function reconcileProgressHistory(list, items) {
  $$(".agent-message:not([data-history-key])", list).forEach((card) => card.remove());
  const existing = new Map($$(".agent-message[data-history-key]", list).map((card) => [card.dataset.historyKey, card]));
  const occurrences = new Map();
  const desired = items.map((item, index) => {
    const identity = item.streamId || JSON.stringify([item.type, item.title, item.text, item.count]);
    const occurrence = (occurrences.get(identity) || 0) + 1;
    occurrences.set(identity, occurrence);
    const key = JSON.stringify([identity, occurrence]);
    let card = existing.get(key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.historyKey = key;
    }
    const content = `<span class="agent-avatar">${agentIdentityGlyph(editorPresentationKind())}</span><div><b>${escapeHtml(item.title)}${item.count > 1 ? `<em class="history-count">×${item.count}</em>` : ""}</b>${["message", "analysis"].includes(item.type) ? `<div class="agent-markdown">${agentMarkdownHtml(item.text)}</div>` : `<p>${escapeHtml(item.text)}</p>`}</div>`;
    if (card.dataset.renderKey !== content) {
      card.innerHTML = content;
      card.dataset.renderKey = content;
    }
    card.className = `agent-message history-${item.type}${index === items.length - 1 ? " latest" : ""}`;
    existing.delete(key);
    return card;
  });
  existing.forEach((card) => card.remove());
  desired.forEach((card, index) => {
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
}

function renderAgentProgress(body, evs, { running = false, rendered = false, n = 0, source = "" } = {}) {
  if (!body) return;
  const preserveHistory = source === "dynamic:0";
  body.classList.toggle("preserve-history", preserveHistory);
  body.classList.remove("is-orchestration");
  body.closest(".outline-agent-turn")?.classList.remove("is-orchestration");
  const allStories = evs
    .filter((ev) => ev.k === "text" || ev.k === "analysis")
    .map((ev) => cleanAgentText(ev.s))
    .filter(Boolean)
    .filter((text, index, all) => index === 0 || text !== all[index - 1]);
  const stories = preserveHistory ? allStories : allStories.slice(-4);
  const historyItems = preserveHistory
    ? progressHistoryItems(evs)
    : pageAgentProgressItems(evs, source, n);
  const actions = summarizeToolEvents(evs);
  const transactions = liveToolTransactions(evs);
  const actionCount = evs.filter((ev) => ev.k === "tool").length;
  const [stateTitle, stateDetail] = agentCurrentState(evs, { running, rendered, n });
  const signature = JSON.stringify([source, running, rendered, stateTitle, stories, historyItems, actions, transactions]);
  if (body.dataset.progressSignature === signature) return;
  const scroller = body.closest(".outline-chat-body") || body;
  const wasNearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const previousTop = scroller.scrollTop;
  const shellKey = JSON.stringify([source, n]);
  if (body.dataset.progressShell !== shellKey || !body.querySelector(".agent-now")) {
    const configHtml = body.id === "agent-progress-console" && n === 0
      ? outlineTaskConfigMarkup(editorPresentationKind(), taskConfigData())
      : "";
    body.dataset.progressShell = shellKey;
    body.innerHTML = `
      <section class="agent-now">
        <span class="agent-now-icon"><i></i></span>
        <div><span class="agent-kicker">当前状态</span><strong></strong><p></p></div>
      </section>
      ${configHtml}
      <section class="agent-story">
        <div class="agent-section-head"><span>${preserveHistory ? "完整制作记录" : "本页制作记录"}</span><em>${preserveHistory ? "按发生顺序持续保留" : "责任 Agent 的判断、执行与结果"}</em></div>
        <div class="agent-story-list"></div>
      </section>
      <details class="agent-actions">
        <summary><span>执行细节</span><em></em><i></i></summary>
        <div class="agent-action-list"></div>
      </details>`;
  }
  body.dataset.progressSignature = signature;
  const now = body.querySelector(".agent-now");
  now.classList.toggle("is-running", running);
  if (now.querySelector("strong").textContent !== stateTitle) now.querySelector("strong").textContent = stateTitle;
  if (now.querySelector("p").textContent !== stateDetail) now.querySelector("p").textContent = stateDetail;

  const storyKey = JSON.stringify(historyItems);
  const storyList = body.querySelector(".agent-story-list");
  if (storyList.dataset.renderKey !== storyKey) {
    storyList.dataset.renderKey = storyKey;
    reconcileProgressHistory(storyList, historyItems);
  }

  const actionKey = JSON.stringify([running, actions, transactions]);
  const actionDetails = body.querySelector(".agent-actions");
  const actionSummary = actionDetails.querySelector("summary em");
  const failedTransactions = transactions.filter((item) => item.result && !item.result.ok).length;
  const openTransactions = transactions.filter((item) => !item.result).length;
  const actionSummaryText = actionCount
    ? `${actionCount} 次事务${openTransactions ? ` · ${openTransactions} 运行中` : ""}${failedTransactions ? ` · ${failedTransactions} 失败` : ""}`
    : "等待执行";
  if (actionSummary.textContent !== actionSummaryText) actionSummary.textContent = actionSummaryText;
  const actionList = body.querySelector(".agent-action-list");
  if (actionList.dataset.renderKey !== actionKey) {
    const openToolIds = new Set($$(".live-transaction[open]", actionList).map((item) => item.dataset.toolUseId));
    actionList.dataset.renderKey = actionKey;
    actionList.innerHTML = transactions.length
      ? transactions.map((transaction) => liveToolTransactionMarkup(transaction, { open: openToolIds.has(transaction.id), active: running })).join("")
      : actions.length ? actions.map((action, index) => `
      <div class="agent-action${index === 0 && running ? " active" : ""}"><span>${index === 0 && running ? "•" : "✓"}</span><b>${escapeHtml(action.label)}</b>${action.hint ? `<i>${escapeHtml(action.hint)}</i>` : ""}${action.count > 1 ? `<em>×${action.count}</em>` : ""}</div>`).join("") : '<div class="agent-action-empty">还没有底层执行记录</div>';
  }
  requestAnimationFrame(() => {
    scroller.scrollTop = wasNearBottom ? scroller.scrollHeight : previousTop;
  });
}

const ORCHESTRATION_ROLE_LABELS = {
  harness: ["后台解析", "↥", "Harness 运行状态 · 非模型消息"],
  orch: ["整体策划", "主", "梳理结构并协调制作"],
  material: ["Material Agent", "材", "解析并整理用户材料"],
  research: ["Research Agent", "研", "阅读材料、检索外部证据并形成知识依据"],
  image: ["Image Agent", "图", "搜索、生成并检查图片素材"],
  review: ["Review Agent", "审", "复核成稿与整体一致性"],
  content: ["内容审计", "审", "核对用户要求、来源与最终页面内容"],
  critic: ["视觉 Critic", "检", "独立检查当前页面像素"],
};

function canonicalOrchestrationAgentKey(value) {
  const raw = String(value || "").trim().replace(/_r\d+$/i, "");
  return ["orch", "orchestrator"].includes(raw.toLowerCase()) ? "orch" : raw;
}

function orchestrationAgentMeta(key) {
  key = canonicalOrchestrationAgentKey(key);
  const group = String(key).match(/^slide(?:_|-)group(?:_|-)(.+)$/);
  if (group) {
    const pages = agentPages(key);
    if (pages.length === 1) return {
      key, role: "slide", label: `第 ${pages[0]} 页制作`, icon: pad2(pages[0]),
      note: "完善本页内容与画面", page: pages[0], pages,
    };
    return {
      key, role: "slide", label: pages.length ? "页面组协作" : "页面制作", icon: pages.length ? "组" : "页",
      note: pages.length ? `统一制作第 ${pages.map((page) => Number(page)).join("、")} 页` : "负责页码尚未获取",
      page: pages[0] || 0, pages,
    };
  }
  const slide = String(key).match(/^slide[_-]0?(\d+)/);
  if (slide) return { key, role: "slide", label: `第 ${Number(slide[1])} 页制作`, icon: pad2(Number(slide[1])), note: "完善本页内容与画面", page: Number(slide[1]) };
  const family = String(key).split(/[_-]/)[0];
  const [label, icon, note] = ORCHESTRATION_ROLE_LABELS[family] || ["协作 Agent", "✦", "推进当前协作任务"];
  return { key, role: family, label, icon, note, page: 0 };
}

function orchestrationAgentGlyph(meta, kind = editorPresentationKind(), className = "orch-agent-glyph") {
  // Dynamic production is intentionally represented by one continuous Agent.
  // Static production has several specialist roles, so its timeline uses a
  // distinct, compact pictogram for each responsibility instead of repeating
  // the same four-square mark on every card.
  if (kind !== "static") return agentIdentityGlyph(kind, className);
  // One visual family: solid rounded modules rather than unrelated outline
  // icons.  The silhouettes differ at a glance while retaining the weight of
  // the MURAL purple-to-mint identity mark.
  const svg = (body) => `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">${body}</svg>`;
  if (meta.role === "research") return svg(`
    <rect x="2.3" y="3.2" width="11.2" height="13.8" rx="3" fill="currentColor" opacity=".22"/>
    <rect x="5.2" y="1.8" width="11.5" height="12.8" rx="3" fill="currentColor" opacity=".46"/>
    <path d="M7.9 5.6h5.8M7.9 8.2h3.9" stroke="currentColor" stroke-width="1.65" stroke-linecap="round"/>
    <circle cx="14.2" cy="14" r="3.55" fill="currentColor"/>
    <circle cx="14.2" cy="14" r="1.25" fill="white" opacity=".94"/>
  `);
  if (meta.role === "material") return svg(`
    <rect x="2.7" y="3" width="14.6" height="3.5" rx="1.75" fill="currentColor" opacity=".42"/>
    <rect x="2.7" y="8.25" width="11.6" height="3.5" rx="1.75" fill="currentColor" opacity=".72"/>
    <rect x="2.7" y="13.5" width="8.2" height="3.5" rx="1.75" fill="currentColor"/>
  `);
  if (meta.role === "image") return svg(`
    <rect x="2.5" y="2.5" width="8.9" height="15" rx="2.6" fill="currentColor"/>
    <rect x="13" y="2.5" width="4.5" height="6.2" rx="1.7" fill="currentColor" opacity=".46"/>
    <rect x="13" y="10.3" width="4.5" height="7.2" rx="1.7" fill="currentColor" opacity=".74"/>
  `);
  if (meta.role === "review") return svg(`
    <rect x="2.2" y="4.8" width="11.7" height="12.7" rx="3" fill="currentColor" opacity=".23"/>
    <rect x="5" y="2.2" width="12.8" height="12.8" rx="3.3" fill="currentColor" opacity=".38"/>
    <path d="m8.2 8.5 2.25 2.25 4.15-4.25" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="15.35" cy="15.3" r="2.45" fill="white"/>
    <path d="m14.2 15.3.75.75 1.55-1.65" stroke="#6258d7" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>
  `);
  if (meta.role === "slide" && /^slide(?:_|-)group/i.test(String(meta.key || ""))) return svg(`
    <rect x="2.2" y="6" width="10.2" height="11.3" rx="2.3" fill="currentColor" opacity=".28"/>
    <rect x="5.1" y="3.5" width="10.2" height="11.3" rx="2.3" fill="currentColor" opacity=".58"/>
    <rect x="8" y="1.2" width="9.8" height="11" rx="2.3" fill="currentColor"/>
  `);
  if (meta.role === "slide") return svg(`
    <rect x="2.5" y="2.5" width="15" height="15" rx="3" fill="currentColor" opacity=".24"/>
    <rect x="5" y="5" width="10" height="4" rx="1.5" fill="currentColor"/>
    <rect x="5" y="10.6" width="4.3" height="4.4" rx="1.4" fill="currentColor" opacity=".72"/>
    <rect x="10.7" y="10.6" width="4.3" height="4.4" rx="1.4" fill="currentColor" opacity=".45"/>
  `);
  if (meta.role === "orch") return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <rect x="3" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
  </svg>`;
  return svg(`<path d="M10 2.7c.7 3.56 3.06 5.92 6.62 6.62-3.56.7-5.92 3.06-6.62 6.62-.7-3.56-3.06-5.92-6.62-6.62C6.94 8.62 9.3 6.26 10 2.7Z" fill="currentColor"/>`);
}

function orchestrationFlatFeed(feed) {
  let fallbackSequence = 0;
  return Object.entries(feed || {}).flatMap(([agent, events]) => (events || []).map((event) => ({
    ...event, agent: canonicalOrchestrationAgentKey(agent),
    order: Number.isFinite(Number(event.stream_start_seq ?? event.seq)) ? Number(event.stream_start_seq ?? event.seq) : fallbackSequence++,
  }))).sort((a, b) => a.order - b.order);
}

function orchestrationEventTime(event) {
  const elapsed = Number(event?.elapsed_s);
  const local = conversationTime(event?.ts || event?.created_at);
  const full = conversationTime(event?.ts || event?.created_at, { withDay: true });
  const grouped = conversationTime(event?.ts || event?.created_at, { withDay: true, bucketMinutes: 5 });
  if (local && full && grouped) {
    return {
      text: local.text,
      minute: grouped.groupKey, groupLabel: grouped.text, iso: local.iso,
      title: `${full.text} · 事件发生时间（浏览器本地时区）${Number.isFinite(elapsed) ? ` · 任务启动后 ${fmtDur(elapsed)}` : ""}`,
    };
  }
  // Compatibility fallback for historical events that have only a bare
  // Harness wall clock. New events always prefer the explicit UTC timestamp
  // above and are localized by the browser.
  const clock = String(event?.clock || "").trim();
  if (/^\d{2}:\d{2}:\d{2}$/.test(clock)) {
    return {
      text: clock,
      minute: clock.slice(0, 5), groupLabel: clock.slice(0, 5),
      title: Number.isFinite(elapsed) ? `历史记录时间 · 任务启动后 ${fmtDur(elapsed)}` : "历史过程记录时间",
    };
  }
  return null;
}

function orchestrationOutputTimeMarkup(value, label = "记录") {
  if (!value?.text) return "";
  return `<time class="orch-output-time" title="${escapeHtml(value.title || value.text)}"><span>${escapeHtml(value.text)}</span><em>${escapeHtml(label)}</em></time>`;
}

function orchestrationAssignmentText(hint) {
  const value = String(hint || "");
  if (/\bMaterial\b/i.test(value)) return "已将附件解析与材料整理分派给 Material Agent。";
  if (/\bResearch\b/i.test(value)) return "已将事实核查与资料研究分派给 Research Agent。";
  if (/\bImage\b/i.test(value)) return "已将整册配图与视觉素材准备分派给 Image Agent。";
  if (/\bReview\b/i.test(value)) return "已将成稿复核与一致性检查分派给 Review Agent。";
  if (/\bSlide\s*\d*/i.test(value)) return "页面制作任务已经安排，多页内容与视觉开始同步推进。";
  return "协作任务已经安排，相关制作环节开始推进。";
}

function agentTimingFor(key) {
  const timings = ed.agentTimings || {};
  return timings[key] || timings[canonicalOrchestrationAgentKey(key)] || null;
}

function agentTimingBadge(timing) {
  const started = conversationTime(timing?.started_at);
  const startedFull = conversationTime(timing?.started_at, { withDay: true });
  const finished = conversationTime(timing?.finished_at);
  const finishedFull = conversationTime(timing?.finished_at, { withDay: true });
  const duration = fmtDur(timing?.duration_s);
  if (!started && !finished && !duration) return "";
  const running = timing.status === "running";
  const failed = timing.status === "failed";
  const partial = timing.status === "partial";
  const waiting = timing.status === "waiting";
  const parts = [];
  if (started) parts.push(`<span>${escapeHtml(started.text)} 发起</span>`);
  if (finished) parts.push(`<span>${escapeHtml(finished.text)} ${failed ? "结束" : partial ? "有界降级" : "完成"}</span>`);
  if (duration) parts.push(`<b>${escapeHtml(`${running ? "已进行" : "耗时"} ${duration}`)}</b>`);
  const titleParts = [];
  if (startedFull) titleParts.push(`发起：${startedFull.text}`);
  if (finishedFull) titleParts.push(`${failed ? "结束" : partial ? "有界降级" : "完成"}：${finishedFull.text}`);
  if (duration) titleParts.push(`${running ? "已进行" : "耗时"}：${duration}`);
  titleParts.push("并行 Agent 的时间可能重叠");
  return `<time class="orch-duration orch-stage-time${running ? " running" : ""}${failed ? " failed" : ""}${partial ? " partial" : ""}${waiting ? " waiting" : ""}" datetime="${escapeHtml(started?.iso || finished?.iso || "")}" title="${escapeHtml(titleParts.join("；"))}">${parts.join("<span class=\"orch-time-separator\">·</span>")}</time>`;
}

function timingAgentLabel(key) {
  const raw = String(key || "");
  const meta = orchestrationAgentMeta(raw);
  if (meta.role === "orch") return "整体策划 Agent";
  if (meta.role === "image") return "Image Agent";
  if (meta.role === "research") return "Research Agent";
  if (meta.role === "material") return "Material Agent";
  if (meta.role === "review") return "Review Agent";
  if (meta.role === "slide") {
    const pages = agentPages(raw);
    const retry = raw.match(/_r(\d+)$/i);
    const scope = raw.replace(/^slide[_-]group[_-]/i, "").replace(/_r\d+$/i, "").replace(/[_-]+/g, " ").trim();
    const pageText = pages.length ? ` · 第 ${pages.join("、")} 页` : (scope ? ` · ${scope}` : "");
    return `页面组 Agent${pageText}${retry ? ` · 重试 ${retry[1]}` : ""}`;
  }
  return meta.label || "协作 Agent";
}

function overallDurationSeconds(timing = ed.overallTiming || {}) {
  const explicitRaw = timing.duration_s;
  const explicit = Number(explicitRaw);
  if (explicitRaw != null && explicitRaw !== "" && Number.isFinite(explicit)) {
    return Math.max(0, explicit);
  }

  // Progress owns the end-to-end job clock (including startup before the first
  // Harness event), and already advances locally between SSE payloads.
  const progressElapsed = currentElapsedSeconds();
  if (Number.isFinite(progressElapsed)) return Math.max(0, progressElapsed);

  // Compatibility fallback for historical trajectories that expose only ISO
  // start/end stamps and never persisted duration_s.
  const started = Date.parse(String(timing.started_at || ""));
  const finished = Date.parse(String(timing.finished_at || ""));
  if (!Number.isFinite(started)) return null;
  if (Number.isFinite(finished)) return Math.max(0, (finished - started) / 1000);
  if (isStaticActiveStatus(ed.status)) return Math.max(0, (Date.now() - started) / 1000);
  return null;
}

function orchestrationTimingMarkup() {
  const timings = Object.entries(ed.agentTimings || {})
    .filter(([, timing]) => timing && timing.duration_s != null && timing.duration_s !== ""
      && Number.isFinite(Number(timing.duration_s)))
    .sort(([, a], [, b]) => String(a.started_at || "").localeCompare(String(b.started_at || "")));
  const overall = ed.overallTiming || {};
  const overallSeconds = overallDurationSeconds(overall);
  const overallDuration = fmtDur(overallSeconds);
  if (!timings.length && !overallDuration) return "";
  const rows = timings.map(([key, timing]) => {
    const meta = orchestrationAgentMeta(key);
    const failed = timing.status === "failed";
    const running = timing.status === "running";
    const completedWithFallback = timing.completion_status === "completed_with_fallback";
    return `<div class="orchestration-timing-agent role-${escapeHtml(meta.role)}${failed ? " failed" : ""}${running ? " running" : ""}" title="${escapeHtml(key)}">
      <i class="orchestration-timing-dot" aria-hidden="true"></i>
      <span>${escapeHtml(timingAgentLabel(key))}</span>
      <b>${escapeHtml(fmtDur(timing.duration_s))}</b>
      <em>${failed ? "未完成" : (running ? "进行中" : (completedWithFallback ? "已完成 · 已采用替代方案" : "已完成"))}</em>
    </div>`;
  }).join("");
  return `<section class="orchestration-timing-card">
    <div class="orchestration-timing-head">
      <span><small>整体用时</small><strong data-overall-elapsed>${escapeHtml(overallDuration || "尚未开始")}</strong></span>
      <em><b>${timings.length}</b> 个 Agent 协同 · 各阶段包含并行重叠</em>
    </div>
    <div class="orchestration-timing-agents">${rows}</div>
  </section>`;
}


function compactPageAgentResult(value) {
  const text = cleanAgentText(value);
  return {
    summary: text.length > 160 ? `${text.slice(0, 160).trimEnd()}…` : text,
    detail: text.length > 160 ? text : "",
  };
}

function compactOrchestrationCurrentDetail(meta, value) {
  return compactPageAgentResult(value).summary || "暂无 Agent 说明。";
}

function specialistToolCount(events, tools) {
  const wanted = new Set(tools);
  return (events || []).filter((event) => event.k === "tool" && wanted.has(event.tool)).length;
}

function specialistToolStep(label, events, tools) {
  const relevant = (events || []).filter(event => ["tool", "tool_result"].includes(event.k) && tools.includes(event.tool));
  const rows = liveToolTransactions(relevant, {limit: Infinity});
  const success = rows.filter(row => row.result?.ok === true).length;
  const failed = rows.filter(row => row.result?.ok === false).length;
  const unmatched = rows.filter(row => !row.result).length + relevant.filter(event => event.k === "tool" && !event.tool_use_id).length;
  const detail = relevant.length ? [
    `${specialistToolCount(relevant, tools)} 次发起记录`,
    `${success} 次成功回执`,
    failed ? `${failed} 次失败回执` : "",
    unmatched ? `${unmatched} 次未见结束回执` : "",
  ].filter(Boolean).join(" · ") : "当前已载入记录未包含此类操作";
  // 这里只汇总当前可见证据，不把调用成功或历史失败推断为阶段完成或失败。
  return specialistStep(label, detail, relevant.length ? "recorded" : "unknown");
}

function specialistStep(label, detail, state) {
  return { label, detail, state };
}

function specialistProgressEntry(meta, events, artifacts = {}) {
  const ordered = (events || []).map((event, index) => ({
    ...event, order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index,
  })).sort((a, b) => a.order - b.order);
  const latestText = [...ordered].reverse().find(event => event.k === "text" && !event.partial)?.s || "";
  const lifecycle = [...ordered].reverse().find(event =>
    ["agent_started", "agent_completed", "agent_failed"].includes(event.event_type));
  const entry = {
    type: "specialist", meta,
    completed: lifecycle?.event_type === "agent_completed",
    failed: lifecycle?.event_type === "agent_failed",
    result: cleanAgentText(latestText),
    order: ordered[0]?.order ?? 0, key: `specialist:${meta.key}`,
    metrics: [], previews: [],
  };
  const step = (label, tools) => specialistToolStep(label, ordered, tools);
  if (meta.role === "review") {
    const result = compactPageAgentResult(latestText);
    return {...entry, result: result.summary, resultDetail: result.detail, steps: [
      step("读取成稿", ["read", "read_file"]),
      step("渲染与看图", ["vision_analyze", "inspect", "inspect_group", "render_slides", "render", "screenshot"]),
      step("修改记录", ["edit", "edit_file", "write", "write_file", "patch"]),
    ]};
  }
  if (meta.role === "research") {
    return {...entry, steps: [
      step("检索公开资料", ["web_search"]),
      step("读取知识与资料", ["web_extract", "web_fetch", "read", "read_file"]),
      specialistStep("研究任务稿", artifacts.output_recorded ? `${artifacts.output_path} 已落盘；不代表研究完成`
        : artifacts.output_path ? `已分配 ${artifacts.output_path}；尚无当前文件记录`
        : "当前未提供本任务研究稿的落盘记录", artifacts.output_recorded ? "recorded" : "unknown"),
    ],
      queries: Array.isArray(artifacts.queries) ? artifacts.queries : [],
      sources: Array.isArray(artifacts.sources) ? artifacts.sources : [],
    };
  }
  if (meta.role === "material") {
    const files = Array.isArray(artifacts.files) ? artifacts.files : [];
    return {...entry, files, steps: [
      specialistStep("材料清单", files.length ? `已记录 ${files.length} 份材料` : "当前未提供材料清单",
        files.length ? "recorded" : "unknown"),
      step("读取与解析记录", ["terminal", "read_file"]),
    ]};
  }
  const imageItems = Array.isArray(artifacts.images) ? artifacts.images : [];
  const assetCount = Number.isInteger(artifacts.count) && artifacts.count >= 0 ? artifacts.count
    : Array.isArray(artifacts.images) ? imageItems.length : null;
  const visionCount = specialistToolCount(ordered, ["inspect", "vision_analyze"]);
  const origins = imageItems.reduce((counts, item) => {
    const key = String(item.origin || "unclassified");
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
  return {...entry, steps: [
    step("读取视觉依据", ["read", "read_file"]),
    step("搜索与生成素材", ["web_search", "web_fetch", "web_extract", "fetch_image", "image_generate"]),
    step("素材看图记录", ["inspect", "vision_analyze"]),
    specialistStep("素材登记", assetCount !== null ? `登记记录中有 ${assetCount} 项素材` : "当前未提供素材登记数量",
      assetCount !== null ? "recorded" : "unknown"),
  ],
    metrics: [assetCount !== null ? `${assetCount} 项登记素材` : "素材数量未记录", `${visionCount} 次看图调用记录`],
    previews: imageItems, contactSheet: artifacts.contact_sheet || null,
    assetCount, visionCount, origins,
  };
}



function appendOrchestratorProgress(updates, text, order, eventTime = null) {
  const original = cleanAgentText(text);
  if (!original || updates.at(-1)?.text === original) return;
  updates.push({text: original, order, eventTime});
}

function slideOwnerSegments(events) {
  const groups = new Map();
  (events || []).filter((event) => !event.partial).forEach((event, index) => {
    const id = String(event.segment_id || "unsegmented");
    const rows = groups.get(id) || [];
    rows.push({
      ...event,
      order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index,
    });
    groups.set(id, rows);
  });
  return [...groups.entries()].map(([segmentId, rows], index) => {
    rows.sort((a, b) => a.order - b.order);
    const terminal = [...rows].reverse().find((event) =>
      ["agent_completed", "agent_failed"].includes(event.event_type));
    const start = rows.find((event) => event.event_type === "agent_started") || rows[0];
    const finish = terminal || rows.at(-1);
    const started = Date.parse(start?.ts || "");
    const finished = Date.parse(finish?.ts || "");
    const patchReason = rows
      .filter((event) => event.k === "tool" && event.tool === "patch")
      .map((event) => String(event.args?.repair_reason || "").trim())
      .find(Boolean) || "";
    return {
      segmentId,
      rows,
      reused: index > 0,
      repairReason: patchReason,
      failed: terminal?.event_type === "agent_failed",
      completed: terminal?.event_type === "agent_completed",
      timing: {
        status: terminal?.event_type === "agent_failed" ? "failed"
          : terminal?.event_type === "agent_completed" ? "complete" : "running",
        started_at: start?.ts || null,
        finished_at: terminal ? finish?.ts || null : null,
        duration_s: Number.isFinite(started) && Number.isFinite(finished)
          ? Math.max(0, Math.round((finished - started) / 100) / 10) : null,
      },
    };
  });
}

function orchestrationEntries(feed, specialistArtifacts = ed.specialistArtifacts || {}) {
  const entries = [];
  const contentAudits = new Map();
  const seenContentEvents = new Set();
  const occurrences = new Map();
  const lastTextByAgent = new Map();
  const all = orchestrationFlatFeed(feed);
  const flat = all.filter((event) => event.k !== "context");
  // 关联执行状态与模型反馈；真实 Critic 自有 Trace，不冒充 Owner 或 Orchestrator 的发言。
  const reviews = new Map();
  all.filter(event => orchestrationAgentMeta(event.agent).role === "critic").forEach(event => {
    const info = event.critic || {};
    if (event.k === "context" && !info.inspection_id) return;
    const identity = info.inspection_id ? [info.inspection_id, info.owner_id || "", info.png_sha256 || ""]
      : [event.agent_id || event.agent, event.segment_id || ""];
    const key = JSON.stringify(identity);
    let review = reviews.get(key);
    if (!review) {
      review = {type: "critic-review", meta: {...orchestrationAgentMeta(event.agent)},
        info: {}, messages: [], state: "pending", order: event.order, key: `critic:${key}`};
      reviews.set(key, review);
      entries.push(review);
    }
    Object.assign(review.info, info);
    const traceAgent = info.trace_agent_id || (event.segment_id ? event.agent_id : "");
    if (traceAgent) {
      review.meta.key = traceAgent;
      review.segment = info.trace_segment_id || event.segment_id || "";
    }
    if (event.event_type?.startsWith("critic_")) {
      review.state = info.completed === true ? "complete"
        : event.event_type === "critic_failed" || (event.event_type === "critic_completed" && info.completed === false) ? "failed" : "pending";
      review.reused = event.event_type === "critic_reused" || info.cache_hit === true;
    }
    if (event.k === "text" && !event.partial && event.s && !review.messages.includes(event.s)) review.messages.push(event.s);
    review.eventTime = orchestrationEventTime(event);
  });
  const toolResults = new Map(flat.filter(event => event.k === "tool_result" && event.tool_use_id)
    .map(event => [`${event.agent}:${event.segment_id}:${event.tool_use_id}`, event]));
  let orchestratorProgress = null;
  const flushOrchestratorProgress = () => {
    if (!orchestratorProgress?.updates?.length) {
      orchestratorProgress = null;
      return;
    }
    entries.push({
      type: "orchestrator-progress",
      meta: orchestratorProgress.meta,
      timing: agentTimingFor("orch"),
      eventTime: orchestratorProgress.updates.at(-1)?.eventTime || null,
      updates: orchestratorProgress.updates,
      order: orchestratorProgress.order,
      key: `orchestrator-progress:${orchestratorProgress.order}`,
    });
    orchestratorProgress = null;
  };
  flat.forEach((event) => {
    const agentKey = canonicalOrchestrationAgentKey(event.agent);
    const rawMeta = orchestrationAgentMeta(agentKey);
    const isOrchestrator = rawMeta.role === "orch" || agentKey === "orch";
    const meta = {...(isOrchestrator ? orchestrationAgentMeta("orch") : rawMeta),
      segment: event.segment_id || "", taskId: event.task_id || ""};
    if (meta.role === "critic") return;
    if (meta.role === "content" && (event.event_type?.startsWith("content_qc_") || event.k === "text")) {
      if (event.partial || (event.event_id && seenContentEvents.has(event.event_id))) return;
      if (event.event_id) seenContentEvents.add(event.event_id);
      flushOrchestratorProgress();
      const isStatus = event.event_type?.startsWith("content_qc_");
      const isStart = event.event_type === "content_qc_started";
      let audit = contentAudits.get(agentKey);
      // 摘要与模型消息的 segment 不同，只在明确的开始/结束边界内归并。
      if (!audit || audit.closed || isStart || (isStatus && audit.segment !== event.segment_id)) {
        audit = {type: "content-audit", meta, findings: [], rawMessages: [], summary: {},
          findingsReceived: false, segment: event.segment_id, closed: false,
          order: event.order, key: `content-audit:${event.event_id || event.order}`};
        entries.push(audit);
        contentAudits.set(agentKey, audit);
      }
      audit.eventTime = orchestrationEventTime(event);
      if (isStatus) {
        audit.eventType = event.event_type;
        try {
          const value = JSON.parse(String(event.s || "").slice(String(event.s || "").indexOf("{")));
          if (value && !Array.isArray(value) && typeof value === "object") audit.summary = value;
        } catch { /* 原始 Trace 保留，格式不完整时不推断审计通过。 */ }
        audit.closed = !isStart;
      } else {
        try {
          const findings = JSON.parse(event.s);
          if (!Array.isArray(findings) || findings.some(row => !row || typeof row.summary !== "string" || !Array.isArray(row.pages))) throw new Error("invalid findings");
          audit.findingsReceived = true;
          for (const row of findings) {
            if (!audit.findings.some(existing => JSON.stringify(existing) === JSON.stringify(row))) audit.findings.push(row);
          }
        } catch {
          if (!audit.rawMessages.includes(event.s)) audit.rawMessages.push(event.s);
        }
      }
      return;
    }
    if (event.partial) {
      flushOrchestratorProgress();
      entries.push({
        type: "stream-preview", meta, text: liveAgentText(event),
        analysis: event.k === "analysis", interrupted: event.phase === "interrupted",
        label: `${event.k === "analysis" ? "分析摘要 · " : ""}${livePreviewLabel(event)}`,
        eventTime: orchestrationEventTime(event), order: event.order,
        key: `stream:${event.stream_id}`,
      });
      return;
    }
    const isCompactionRuntime = ["agent_context_compacted", "agent_context_compaction_failed"].includes(event.event_type)
      || (event.k === "provider" && event.phase === "started" && event.request_kind === "context_compaction");
    if (event.event_type?.startsWith("attachment_parse_") || isCompactionRuntime) {
      flushOrchestratorProgress();
      const [action, detail] = runtimeEventState(event);
      entries.push({type: "runtime", meta, action, detail,
        runtimeKind: isCompactionRuntime ? "compaction" : "background",
        eventTime: orchestrationEventTime(event), order: event.order,
        key: `runtime:${event.event_id || event.order}`});
      return;
    }
    if (event.k === "provider" && ["failed", "failover"].includes(event.phase)) {
      flushOrchestratorProgress();
      const action = event.phase === "failover" ? "Provider 通道切换" : "Provider 请求失败";
      const detail = event.phase === "failover"
        ? `已切换到 ${event.channel_id || "备用通道"}，从闭合边界继续。`
        : [event.status_code ? `HTTP ${event.status_code}` : event.error_kind || "上游请求失败",
           event.error_summary, event.duration_ms ? `${event.duration_ms} ms` : ""].filter(Boolean).join(" · ");
      entries.push({
        type: "provider", meta, action, detail,
        eventTime: orchestrationEventTime(event), order: event.order,
        key: `provider:${event.event_id || event.order}`,
      });
      return;
    }
    if (meta.role === "content" && event.k === "status") return;
    if (isOrchestrator && event.k === "analysis") {
      flushOrchestratorProgress();
      const text = liveAgentText(event);
      if (text) entries.push({
        type: "analysis", meta, text,
        eventTime: orchestrationEventTime(event), order: event.order,
        key: `analysis:${event.event_id || event.order}`,
      });
      return;
    }
    if (isOrchestrator && event.k === "text") {
      const text = liveAgentText(event);
      if (!text) return;
      if (event.event_id) {
        flushOrchestratorProgress();
        entries.push({
          type: "message", meta, text,
          eventTime: orchestrationEventTime(event), order: event.order,
          key: `message:${event.event_id}`,
        });
        return;
      }
      if (lastTextByAgent.get(agentKey) === text) return;
      lastTextByAgent.set(agentKey, text);
      if (!orchestratorProgress) orchestratorProgress = { meta, order: event.order, updates: [] };
      appendOrchestratorProgress(orchestratorProgress.updates, text, event.order, orchestrationEventTime(event));
      return;
    }
    if (isOrchestrator && event.k === "tool" && event.tool === "delegate_task") {
      flushOrchestratorProgress();
      entries.push({ type: "assignment", meta, timing: agentTimingFor("orch"), eventTime: orchestrationEventTime(event), text: orchestrationAssignmentText(event.hint), order: event.order, key: `assign:${event.order}` });
      return;
    }
    if (isOrchestrator && event.k === "tool") {
      flushOrchestratorProgress();
      const [action, detail] = toolProgress(event);
      const hint = friendlyToolHint(event);
      entries.push({
        type: "orchestrator-action", meta, timing: agentTimingFor("orch"), eventTime: orchestrationEventTime(event),
        action, detail: hint ? `${detail}：${hint}` : detail,
        transaction: event.tool_use_id ? {id: event.tool_use_id, tool: event.tool, agent: meta.key, use: event,
          result: toolResults.get(`${event.agent}:${event.segment_id}:${event.tool_use_id}`) || null} : null,
        artifactRels: workflowArtifactRelsFromEvents([event], meta),
        order: event.order, key: `orchestrator-action:${event.order}`,
      });
      return;
    }
    if (isOrchestrator) return;
    // A specialist taking over marks a real phase boundary. A later
    // Orchestrator response therefore starts a new card rather than extending
    // a stale planning card from before the delegation.
    flushOrchestratorProgress();
    if (meta.role === "slide") return;
    if (["research", "image", "material", "review"].includes(meta.role)) return;
    if (event.k === "text") {
      const text = cleanAgentText(event.s);
      if (!text || lastTextByAgent.get(agentKey) === text) return;
      lastTextByAgent.set(agentKey, text);
      const occurrenceKey = `${agentKey}\u0000${text}`;
      const occurrence = (occurrences.get(occurrenceKey) || 0) + 1;
      occurrences.set(occurrenceKey, occurrence);
      entries.push({ type: "message", meta, timing: agentTimingFor(event.agent), eventTime: orchestrationEventTime(event), text, order: event.order, key: JSON.stringify([occurrenceKey, occurrence]) });
    }
  });
  flushOrchestratorProgress();
  Object.entries(feed || {}).forEach(([agent, events]) => {
    events = (events || []).filter((event) => !event.partial && event.k !== "context");
    const meta = orchestrationAgentMeta(agent);
    if (["research", "image", "material", "review"].includes(meta.role) && (events || []).length) {
      const agentArtifacts = specialistArtifacts.agents?.[meta.key];
      const artifacts = agentArtifacts || (meta.role === "image" ? {} : (specialistArtifacts[meta.role] || {}));
      const entry = specialistProgressEntry(meta, events, artifacts);
      entry.artifactRels = workflowArtifactRelsFromEvents(events, meta);
      entry.segment = events.at(-1)?.segment_id || "";
      entry.taskId = events.at(-1)?.task_id || "";
      entry.timing = agentTimingFor(agent);
      if (entry.timing?.status) {
        entry.completed = entry.timing.status === "complete";
        entry.failed = entry.timing.status === "failed";
      }
      entries.push(entry);
      return;
    }
    if (meta.role !== "slide" || !(events || []).length) return;
    slideOwnerSegments(events).forEach((segment) => {
      const latestTool = [...segment.rows].reverse().find((event) => event.k === "tool");
      const latestText = [...segment.rows].reverse()
        .map((event) => event.k === "text" ? cleanAgentText(event.s) : "")
        .find(Boolean) || "";
      const isRepair = segment.reused;
      const [defaultAction] = latestTool ? toolProgress(latestTool) : ["理解本页任务"];
      const action = segment.repairReason ? "按整册检查反馈修订页面"
        : segment.reused ? "续接原 Owner 完成页面" : defaultAction;
      const completed = segment.completed;
      const result = compactPageAgentResult(latestText, { completed, page: meta.page });
      entries.push({
        type: isRepair ? "repair" : "slide",
        meta,
        segment: segment.segmentId === "unsegmented" ? "" : segment.segmentId,
        taskId: segment.rows.find(event => event.task_id)?.task_id || "",
        action,
        repairReason: segment.repairReason,
        reused: segment.reused,
        result: result.summary,
        resultDetail: result.detail,
        completed,
        artifactRels: workflowArtifactRelsFromEvents(segment.rows, meta),
        timing: segment.timing,
        failed: segment.failed,
        order: segment.rows[0].order,
        lastOrder: segment.rows.at(-1).order,
        key: `slide:${agent}:${segment.segmentId}`,
      });
    });
  });
  return { flat, entries: entries.sort((a, b) => a.order - b.order) };
}

function specialistGalleryMarkup(items, { artifactMode = "inline", modifier = "" } = {}) {
  const list = (Array.isArray(items) ? items : []).filter(item => item.path);
  if (artifactMode === "links") {
    const links = list.map(item => workflowArtifactLinkMarkup(item.path, {
      name: item.name, kind: "image", size: item.size,
      updated_at: item.updated_at || item.mtime, group: "Assets",
    })).filter(Boolean).join("");
    return links ? `<div class="orchestration-artifact-links${modifier ? ` ${modifier}` : ""}">${links}</div>` : "";
  }
  return list.length ? `<div class="specialist-gallery${list.length === 1 ? " single" : ""}${modifier ? ` ${modifier}` : ""}">${list.map(item => {
    const rel = String(item.path);
    const version = Number(item.mtime || 0);
    const full = `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}/file?rel=${encodeURIComponent(rel)}`;
    const thumb = item.legacy ? `${full}&v=${version}` : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${encodeURIComponent(ed.id)}/asset-thumbnail?rel=${encodeURIComponent(rel)}&v=${version}`;
    const label = String(item.name || rel.split("/").at(-1)).replace(/\.[^.]+$/, "").replace(/^generated-[a-f0-9]+$/i, "生成素材").replace(/^contact-sheet$/i, "素材总览");
    const origins = { downloaded: "网络图片 · 已下载", generated: "AI 生成", material: "用户材料", derived: "派生素材", unclassified: "来源未登记" };
    const origin = String(item.origin || "unclassified");
    const source = item.source_url || item.source_path || item.generator_model || item.parent_asset || "";
    const badge = item.legacy || /contact-sheet/i.test(String(item.name || "")) ? "" : `<em class="asset-origin ${escapeHtml(origin)}" title="${escapeHtml(source)}">${escapeHtml(origins[origin] || origins.unclassified)}</em>`;
    return `<a href="${full}" target="_blank" rel="noopener" title="查看原图"><img src="${thumb}" loading="lazy" decoding="async" alt="${escapeHtml(label)}" onerror="this.hidden=true;this.closest('a').classList.add('preview-unavailable')"><span>${escapeHtml(label)}</span>${badge}</a>`;
  }).join("")}</div>` : "";
}

function renderWorkflowImageOutputs() {
  const host = $("#workflow-image-outputs");
  if (!host) return;
  const key = canonicalOrchestrationAgentKey(ed.workflowTraceAgent);
  const images = ed.specialistArtifacts?.agents?.[key]?.images || [];
  const html = (ed.workflowTracePayload?.source || ed.workflowTraceSource || "messages") === "messages"
    && orchestrationAgentMeta(key).role === "image" ? specialistGalleryMarkup(images) : "";
  host.hidden = !html;
  if (host.dataset.renderKey !== html) {
    host.dataset.renderKey = html;
    host.innerHTML = html ? `<summary>Image Owner 已产出图片 <small>该 Owner 的素材 · 跨执行段</small></summary>${html}` : "";
  }
}

function orchestrationEntryContent(entry, { artifactMode = "inline" } = {}) {
  const meta = entry.meta;
  const owner = entry.ownerNode;
  const ownerStatus = owner ? workflowStatusLabel(owner.status) : "";
  const ownerProgress = owner ? [owner.progress || owner.code, owner.repairs ? `修订回流 ×${owner.repairs}` : ""].filter(Boolean).join(" · ") : "";
  const ownerDetails = owner ? `<div class="owner-progress-toolbar"><span>${escapeHtml(ownerProgress)}</span><button type="button" data-embedded-agent="true" data-owner-node-id="${escapeHtml(owner.id)}" data-agent-key="${escapeHtml(owner.agentKey)}" data-workflow-task-id="${escapeHtml(owner.taskId)}">查看迭代</button></div>
    <p class="owner-progress-activity">${escapeHtml(owner.activity?.title || owner.detail)}</p>
    <details class="specialist-phase" data-detail-key="owner-context"><summary class="specialist-phase-head"><b>任务说明与依赖</b><i class="specialist-phase-chevron" aria-hidden="true"></i></summary><div class="specialist-feedback-body"><p>${escapeHtml(owner.summary)}</p><p>${escapeHtml(owner.detail)}</p><small>${escapeHtml(owner.identity)}</small></div></details>` : "";
  const stageMarkup = steps => `<div class="specialist-steps">${steps.map(step => `<div class="specialist-step ${step.state}"><i></i><span><b>${escapeHtml(step.label)}</b><small>${escapeHtml(step.detail)}</small></span></div>`).join("")}</div>
    <p class="owner-progress-note">工具统计仅覆盖当前已载入记录，不代表完整历史或阶段完成；执行结束不代表内容与视觉质量通过。</p>`;
  const footerTime = ["specialist", "slide", "repair"].includes(entry.type) ? agentTimingBadge(entry.timing) : "";
  const avatar = `<span class="orch-avatar ${escapeHtml(meta.role)}" aria-hidden="true">
    ${orchestrationAgentGlyph(meta, editorPresentationKind(), "orch-agent-glyph")}
  </span>`;
  const artifactLinks = artifactMode === "links" && entry.artifactRels?.length
    ? `<div class="orchestration-artifact-links">${entry.artifactRels.map((rel) => workflowArtifactLinkMarkup(rel)).join("")}</div>`
    : "";
  if (entry.type === "critic-review") {
    const info = entry.info;
    const feedback = info.body || entry.messages.join("\n\n");
    const status = entry.state === "complete" ? "反馈已返回" : entry.state === "failed" ? "评审执行未完成" : "等待公开反馈";
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>独立视觉 Critic${info.page ? ` · P${escapeHtml(info.page)}` : ""}</b><span>${status}</span></div>
      <p>${escapeHtml(info.owner_id ? `对应 Owner：${info.owner_id}` : "对应 Owner 未记录")} · ${entry.reused ? "复用同版像素反馈" : "单页外部检查"}，不是 Orchestrator 的整册复核。</p>
      ${feedback ? `<details data-detail-key="critic-feedback"><summary>查看完整反馈</summary><div class="agent-markdown">${agentMarkdownHtml(feedback)}</div></details>` : ""}
      ${info.incomplete_reason ? `<p>${escapeHtml(info.incomplete_reason)}</p>` : ""}
      <small>点击卡片查看此 Critic 的 Trace；执行结束不代表页面通过。</small>
    </div>`;
  }
  if (entry.type === "content-audit") {
    const summary = entry.summary;
    const completed = summary.status === "completed";
    const count = Number.isInteger(summary.finding_count) && summary.finding_count >= 0 ? summary.finding_count : null;
    const hasFindings = entry.findings.length > 0 || count > 0;
    const title = completed && summary.passed === true
      ? (hasFindings ? "通过，但有提示" : count === 0 ? "通过 · 未发现内容问题" : "通过 · 发现数量未记录")
      : completed && summary.passed === false ? "未通过"
      : entry.eventType === "content_qc_failed" || summary.status === "incomplete" ? "审计未完成"
      : entry.closed ? "审计结果待确认" : "审计进行中";
    const levels = {critical: "严重", major: "重要", minor: "轻微", advisory: "建议"};
    const kinds = {factual_conflict: "事实与来源问题", coverage_gap: "需求覆盖缺口", off_topic: "内容偏题", internal_contradiction: "内部矛盾", citation_gap: "引用缺口"};
    const counts = Object.entries(levels).map(([level, label]) => {
      const n = entry.findings.filter(row => row.severity === level).length;
      return n ? `${n} 项${label}` : "";
    }).filter(Boolean);
    const metrics = [summary.reviewer_model, typeof summary.elapsed_s === "number" ? `耗时 ${summary.elapsed_s.toFixed(1)} 秒` : "",
      count != null ? `${count} 项发现` : "发现总数未记录", ...counts].filter(Boolean).join(" · ");
    const note = completed && summary.passed === true
      ? "本次未触发阻断条件；通过不代表提示已修复。修复状态需以后续真实记录为准。"
      : "执行完成与审计通过不同；发现列表不是问题已修复的证明。";
    const missing = (!entry.findingsReceived && count !== 0) || (count != null && count !== entry.findings.length) || entry.rawMessages.length > 0;
    return `${avatar}<div class="orch-entry-copy content-audit-card">
      <div class="orch-entry-head"><b>内容审计</b><span>${escapeHtml(title)}</span></div>
      <p class="content-audit-meta">${escapeHtml(metrics)}</p><p>${escapeHtml(note)}</p>
      ${summary.incomplete_reason ? `<p class="content-audit-warning">${escapeHtml(summary.incomplete_reason)}</p>` : ""}
      ${missing ? `<p class="content-audit-warning">发现明细尚未完整获取（当前 ${entry.findings.length} 项）；不据此判断无问题。</p>` : ""}
      <div class="content-audit-findings">${entry.findings.map(row => `<section class="content-audit-finding">
        <header><b>${escapeHtml(levels[row.severity] || "未标注级别")}</b><span>${escapeHtml(row.pages.length ? row.pages.map(page => `P${page}`).join("、") : "整册 / 未指定页面")} · ${escapeHtml(kinds[row.kind] || row.kind || "内容问题")}</span></header>
        <p>${escapeHtml(row.summary)}</p></section>`).join("")}</div>
      ${entry.rawMessages.length ? `<details data-detail-key="audit-unparsed"><summary>未解析的审计文本</summary><pre>${escapeHtml(entry.rawMessages.join("\n\n"))}</pre></details>` : ""}
      ${footerTime ? `<div class="orch-entry-foot">${footerTime}</div>` : ""}</div>`;
  }
  if (entry.type === "specialist") {
    const status = ownerStatus || (entry.failed ? "失败" : entry.completed ? "已完成" : "进行中");
    const galleryMarkup = (items, modifier = "") => {
      return specialistGalleryMarkup(items, {modifier, artifactMode: meta.role === "image" ? "inline" : artifactMode});
    };
    const previews = galleryMarkup(entry.previews);
    const result = entry.result ? `<div class="specialist-result agent-markdown">${agentMarkdownHtml(entry.result)}</div>` : "";
    const specialistFeedback = (role, title, hint, content) => content ? `<details class="specialist-phase specialist-feedback-details specialist-${role}-feedback-details" data-detail-key="${escapeHtml(role)}-feedback">
        <summary class="specialist-phase-head"><b>${escapeHtml(title)}</b><span>${escapeHtml(hint)}</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-feedback-body">${content}</div>
      </details>` : "";
    const researchSources = meta.role === "research" && entry.sources?.length ? `<details class="specialist-phase research-source-phase specialist-research-details" data-detail-key="research-sources">
        <summary class="specialist-phase-head"><b>来源记录</b><span>${entry.sources.length} 个来源</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-research-body">
          ${entry.queries?.length ? `<div class="specialist-query-list">${entry.queries.map((query) => `<span>${escapeHtml(query)}</span>`).join("")}</div>` : ""}
          <div class="specialist-source-list">${entry.sources.map((source) => `<a href="${escapeHtml(source.url || "#")}" target="_blank" rel="noopener noreferrer"><b>${escapeHtml(source.title || source.url || "资料来源")}</b><small>${escapeHtml(source.url || "")}</small></a>`).join("")}</div>
        </div>
      </details>` : "";
    const materialFiles = meta.role === "material" && entry.files?.length ? `<details class="specialist-phase material-file-phase specialist-material-details" data-detail-key="material-files">
        <summary class="specialist-phase-head"><b>材料记录</b><span>${entry.files.length} 份</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-file-list">${entry.files.map((file) => `<span><i>${escapeHtml(String(file.kind || "file").toUpperCase())}</i><b>${escapeHtml(file.name || "未命名材料")}</b><em>${escapeHtml(file.coverage || file.status || "处理中")}</em></span>`).join("")}</div>
      </details>` : "";
    const reviewPhases = meta.role === "review" ? `<div class="specialist-phase review-live-phase">
        <div class="specialist-phase-head"><b>Agent 复核反馈</b><span>${entry.completed ? "执行已结束" : "随记录更新"}</span></div>
        <div class="specialist-review-live agent-markdown">${agentMarkdownHtml(entry.result || "当前未提供 Agent 复核说明。")}</div>
        ${entry.resultDetail ? `<details class="specialist-review-details" data-detail-key="review-full"><summary class="specialist-phase-head"><b>查看完整复核说明</b><i class="specialist-phase-chevron" aria-hidden="true"></i></summary><div class="specialist-review-body agent-markdown">${agentMarkdownHtml(entry.resultDetail)}</div></details>` : ""}
      </div>` : "";
    const imagePhases = meta.role === "image" ? `<div class="specialist-phase material-phase">
        <div class="specialist-phase-head"><b>图片登记与预览</b><span>${entry.assetCount != null ? `${entry.assetCount} 项登记素材` : "素材数量未记录"}</span></div>
        ${previews}
      </div>
      ${specialistFeedback("image", "查看完整素材说明", "Agent 原始说明与已登记联系表", `
        ${galleryMarkup(entry.contactSheet ? [entry.contactSheet] : [], "contact-sheet-gallery")}
        ${result}
      `)}` : (meta.role === "review" ? reviewPhases : (meta.role === "research"
        ? `${researchSources}${specialistFeedback("research", "查看完整研究说明", "结论、核验与未决事项", result)}`
        : `${materialFiles}${previews}${specialistFeedback("material", "查看完整材料说明", "解析、覆盖与问题反馈", result)}`));
    return `${avatar}<div class="orch-entry-copy specialist-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i><em class="${entry.failed ? "failed" : entry.completed ? "done" : "running"}">${status}</em></div>
      ${ownerDetails}${stageMarkup(entry.steps)}${imagePhases}${artifactLinks}
      ${footerTime ? `<div class="orch-entry-foot">${footerTime}</div>` : ""}
    </div>`;
  }
  if (entry.type === "slide") {
    const status = ownerStatus || (entry.failed ? "失败" : entry.completed ? "已完成" : "正在执行");
    const steps = entry.steps || slideProgressSteps(meta, (ed.feed?.[meta.key] || []).filter(event => !entry.segment || event.segment_id === entry.segment));
    const detail = entry.resultDetail || entry.result;
    const previews = (meta.pages || [meta.page]).filter(page => ed.rendered?.has(page));
    const outputs = previews.length ? `<div class="specialist-phase"><div class="specialist-phase-head"><b>页面预览</b><span>${previews.length} 页已有 PNG</span></div><div class="specialist-gallery">${previews.map(page => `<button type="button" data-embedded-agent="true" data-workflow-page="${page}"><img src="${fileUrl(page)}" alt="P${pad2(page)}" loading="lazy"><span>P${pad2(page)}</span></button>`).join("")}</div></div>` : "";
    return `${avatar}<div class="orch-entry-copy specialist-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i><em class="${entry.failed ? "failed" : entry.completed ? "done" : "running"}">${status}</em></div>
      ${ownerDetails}${stageMarkup(steps)}${outputs}
      ${detail ? `<details class="specialist-phase specialist-feedback-details" data-detail-key="slide-feedback"><summary class="specialist-phase-head"><b>查看完整制作说明</b><i class="specialist-phase-chevron" aria-hidden="true"></i></summary><div class="specialist-feedback-body agent-markdown">${agentMarkdownHtml(detail)}</div></details>` : ""}${artifactLinks}
      ${footerTime ? `<div class="orch-entry-foot">${footerTime}</div>` : ""}
    </div>`;
  }
  if (entry.type === "repair") {
    const hasIssue = !!entry.repairReason;
    const status = entry.failed ? (hasIssue ? "修复失败" : "续接失败")
      : entry.completed ? (hasIssue ? "修复执行已结束" : "续接执行已结束")
        : (hasIssue ? "正在修复" : "正在续接");
    const detail = entry.resultDetail ? `<details class="orch-slide-detail"><summary>查看完整修复说明<i></i></summary><div class="agent-markdown">${agentMarkdownHtml(entry.resultDetail)}</div></details>` : "";
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>重新拉起原 Owner</i><em class="${entry.failed ? "failed" : entry.completed ? "done" : "running"}">${status}</em></div>
      <div class="owner-repair-callout"><b>${hasIssue ? "发现问题后重新拉起原 Owner" : "继续由原 Owner 负责"}</b><p>${escapeHtml(entry.repairReason || "原 Owner 已重新进入执行段，继续完成该负责页面。")}</p></div>
      <div class="orch-slide-flow"><span><i></i><b>${hasIssue ? "修复" : "续接"}</b><em>${escapeHtml(entry.action)}</em></span>${entry.result ? `<span class="result"><i></i><b>${hasIssue ? "复验结果" : "执行结果"}</b><em>${agentStoryHtml(entry.result)}</em></span>` : ""}</div>${detail}${artifactLinks}
      ${footerTime ? `<div class="orch-entry-foot">${footerTime}</div>` : ""}
    </div>`;
  }
  if (entry.type === "orchestrator-progress") {
    const updates = Array.isArray(entry.updates) ? entry.updates : [];
    const latest = updates.at(-1)?.text || "正在梳理演示结构。";
    const history = updates.slice(0, -1);
    const historyMarkup = history.length ? `<details class="orch-progress-history">
      <summary>查看已合并的 ${history.length} 条阶段进展<i aria-hidden="true"></i></summary>
      <ol>${history.map((update) => `<li><p>${escapeHtml(update.text)}</p>${orchestrationOutputTimeMarkup(update.eventTime)}</li>`).join("")}</ol>
    </details>` : "";
    return `<div class="orch-entry-copy orch-plain-message">
      <div class="agent-markdown"><p>${escapeHtml(latest)}</p></div>
      ${historyMarkup}
    </div>`;
  }
  if (entry.type === "orchestrator-action") {
    if (entry.transaction) return `<div class="orch-entry-copy orch-tool-message">
      ${liveToolTransactionMarkup(entry.transaction, {compact: true, active: isStaticActiveStatus(ed.status)})}
      ${artifactLinks}</div>`;
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i></div>
      <div class="orch-slide-flow"><span><i></i><b>${escapeHtml(entry.action || "推进任务")}</b><em>${escapeHtml(entry.detail || "继续推进整套演示。")}</em></span></div>${artifactLinks}
    </div>`;
  }
  if (["provider", "runtime"].includes(entry.type)) {
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${entry.type === "runtime" ? entry.runtimeKind === "compaction" ? "运行时上下文维护 · 非任务输出" : "后台状态 · 非模型消息" : "Provider 恢复"}</i></div>
      <div class="orch-slide-flow"><span><i></i><b>${escapeHtml(entry.action)}</b><em>${escapeHtml(entry.detail)}</em></span></div>
    </div>`;
  }
  if (entry.type === "analysis" || (entry.type === "stream-preview" && entry.analysis)) {
    const label = livePreviewLabel({partial: entry.type === "stream-preview", phase: entry.interrupted ? "interrupted" : ""}, {thinking: true});
    return `<div class="orch-entry-copy"><details class="orch-thinking-fold" data-detail-key="analysis-${escapeHtml(entry.key)}">
      <summary title="仅展示模型返回的思考摘要；已完成指本段摘要已返回，不代表整个任务结束。">${meta.role !== "orch" ? `${escapeHtml(meta.label)} · ` : ""}${escapeHtml(label)}</summary><div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div>
      </details></div>`;
  }
  if (entry.type === "stream-preview") {
    return `<div class="orch-entry-copy orch-plain-message">
      <small class="orch-stream-label">${meta.role !== "orch" ? `${escapeHtml(meta.label)} · ` : ""}${escapeHtml(entry.label)}</small>
      <div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div>
    </div>`;
  }
  if (entry.type === "message" && meta.role === "orch") {
    return `<div class="orch-entry-copy orch-plain-message"><div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div></div>`;
  }
  return `${avatar}<div class="orch-entry-copy">
    <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i></div>
    ${entry.type === "assignment" ? `<p>${escapeHtml(entry.text)}</p>` : `<div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div>`}
  </div>`;
}

function reconcileOrchestrationEntries(list, entries, { artifactMode = "inline" } = {}) {
  const existing = new Map($$(".orch-entry", list).map((card) => [card.dataset.entryKey, card]));
  let previousMinute = "";
  entries.forEach((entry, index) => {
    let card = existing.get(entry.key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.entryKey = entry.key;
    }
    card.className = `orch-entry ${entry.type}${entry.type === "slide" ? " specialist" : ""} role-${entry.meta.role}`;
    if (artifactMode === "links" && entry.meta.key) {
      card.dataset.messageAgent = entry.meta.key;
      card.dataset.messageSegment = entry.segment || entry.meta.segment || "";
      card.dataset.messageTask = entry.taskId || entry.meta.taskId || "";
      card.tabIndex = 0;
      card.setAttribute("role", "link");
      card.setAttribute("aria-label", `查看 ${entry.meta.label} 的消息，并选中协作图卡片`);
    }
    const time = entry.eventTime;
    const minute = time?.minute || time?.text?.slice(0, 5);
    const heading = minute && minute !== previousMinute
      ? `<time class="orch-time-group"${time.iso ? ` datetime="${escapeHtml(time.iso)}"` : ""} title="${escapeHtml(time.title || time.text)}">${escapeHtml(time.groupLabel || minute)}</time>` : "";
    if (minute) previousMinute = minute;
    card.title = time?.title || "";
    const content = heading + orchestrationEntryContent(entry, { artifactMode });
    if (card.dataset.renderKey !== content) {
      const openDetailKeys = new Set([...card.querySelectorAll("details[data-detail-key][open]")]
        .map((detail) => detail.dataset.detailKey));
      card.dataset.renderKey = content;
      card.innerHTML = content;
      card.querySelectorAll("details[data-detail-key]").forEach((detail) => {
        if (openDetailKeys.has(detail.dataset.detailKey)) detail.setAttribute("open", "");
      });
    }
    existing.delete(entry.key);
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
  existing.forEach((card) => card.remove());
}

function muralMakingView() {
  const tasks = (ed.muralRuntime?.tasks || []).filter(task => task.role === "slide");
  const total = Math.max(0, Number(ed.total) || 0);
  const active = isStaticActiveStatus(ed.status);
  const rows = Array.from({length: total}, (_, index) => {
    const page = index + 1;
    const selfAuthored = ed.pageAgents?.[String(page)] === "orch";
    const task = tasks.filter(item => (item.pages || []).includes(page)).at(-1);
    let state = selfAuthored ? (ed.status === "completed" && ed.artifactStatus === "complete" ? "complete" : "observed")
      : task ? workflowExecutionStatus(task.status) : "not_started";
    if (!active && ["running", "queued", "resource_wait", "waiting", "waiting_dependencies", "not_started"].includes(state)) state = "interrupted";
    const preview = !!ed.rendered?.has(page);
    return {page, state, preview, label: selfAuthored ? `主 Agent 制作 · ${state === "complete" ? "交付已验证" : preview ? "已有预览" : "已登记责任"}`
      : task ? workflowStatusLabel(state)
      : preview ? "已有预览 · 检查状态未记录" : active ? "等待页面任务" : "暂无页面产物"};
  });
  return {rows, total, active, previews: rows.filter(row => row.preview).length,
    status: ed.status === "not_started" ? "尚未开始生成" : ed.status === "completed" ? "本轮制作已结束" : active ? "演示文稿制作中" : "本轮制作已暂停或结束"};
}

function muralMakingMarkup(view, hero = false, { checklist = true } = {}) {
  const progress = view.total ? `${view.previews} / ${view.total}` : "—";
  const percent = view.total ? Math.round(view.previews / view.total * 100) : 0;
  const current = view.rows.filter(row => row.state === "running").map(row => `P${row.page}`).join("、");
  return `${hero ? `<section class="mural-making-hero${view.active ? " active" : ""}" aria-label="${view.loadingRecords ? "MURAL 任务加载" : "MURAL 制作进度"}">
    <header><span class="ce-text">${escapeHtml(view.status)}</span>${view.loadingRecords ? "" : `<span>${progress} 页已有预览</span>`}</header>
    <div class="mural-making-brand"><img src="${globalThis.STUDIO_BASE_PATH || ""}/static/logo.svg" alt="MURAL Logo"><strong>MURAL</strong>
      <p>${escapeHtml(view.loadingRecords ? view.active ? "正在载入页面、消息与执行轨迹" : "未能读取任务记录，请重试" : !view.active ? "当前未继续生成，请查看任务记录与后续操作" : current ? `${current} 正在制作` : view.total ? "页面按实际任务并行推进" : "正在规划 · 页数尚未确定")}</p>
      ${view.loadingRecords ? "" : `<b>${progress}<small>页已有预览</small></b>`}
      ${view.total ? `<progress value="${view.previews}" max="${view.total}" aria-label="已生成预览页数"></progress>` : ""}
      <small>${view.loadingRecords ? "读取已有任务记录，不代表重新开始生成" : view.total ? `预览覆盖率 ${percent}% · 不代表视觉检查或整册验收通过` : "没有页数记录时不显示估算百分比"}</small>
    </div></section>` : ""}
    ${view.loadingRecords || !checklist ? "" : `<details class="mural-page-checklist" data-detail-key="page-checklist" open><summary><b>演示文稿制作</b><span>${progress} 页已有预览</span></summary>
      ${view.rows.length ? `<ol>${view.rows.map(row => `<li class="state-${escapeHtml(row.state)}"><span aria-hidden="true">${row.state === "complete" ? "✓" : row.state === "running" ? "●" : ["failed", "dependency_failed", "start_failed"].includes(row.state) ? "!" : "○"}</span><b>第 ${row.page} 页</b><small>${escapeHtml(row.label)}</small></li>`).join("")}</ol>` : '<p>分页计划尚未生成，等待真实页面任务。</p>'}
    </details>`}`;
}

function orchestrationMainView(feed, specialistArtifacts = ed.specialistArtifacts || {}) {
  const { flat, entries } = orchestrationEntries(feed, specialistArtifacts);
  // 同一 Owner 保留一个概览并更新到最新段；局部修复不隐藏卡片，续段不冒充新委派。
  const owners = new Map();
  entries.forEach(entry => {
    if (!["slide", "repair"].includes(entry.type)) return;
    const key = entry.meta.key;
    const previous = owners.get(key);
    const latest = previous && previous.lastOrder > entry.lastOrder ? previous : entry;
    owners.set(key, {
      ...latest, type: "slide", key: `slide-owner:${key}`,
      order: previous?.order ?? entry.order,
      timing: agentTimingFor(key) || latest.timing,
      artifactRels: [...new Set([...(previous?.artifactRels || []), ...(entry.artifactRels || [])])],
    });
  });
  const visible = [...entries.filter(entry => entry.meta.role === "orch"
    || entry.type === "specialist"
    || (entry.type === "runtime" && (entry.meta.role === "harness" || entry.runtimeKind === "compaction"))), ...owners.values()];
  // 委派回执只保留工具事务；每个真实 Owner 在下方维护一张阶段卡，尚未启动也可见。
  const progress = new Map();
  const include = (node, order = 0) => {
    const key = node.agentKey || node.taskId;
    const old = progress.get(key);
    const currentId = ed.agentTimings?.[node.agentKey]?.task_id;
    if (old && old.ownerNode.taskId === currentId && node.taskId !== currentId) return;
    const previous = visible.find(entry => ["slide", "specialist"].includes(entry.type) && entry.meta.key === node.agentKey);
    progress.set(key, {...workflowOwnerProgressEntry(node, previous), order: old?.order ?? previous?.order ?? order});
  };
  entries.filter(entry => entry.transaction?.tool === "delegate_submit").forEach(entry =>
    workflowDelegationNodes(entry.transaction).forEach(node => include(node, entry.order + .1)));
  workflowTaskGraph(ed.muralRuntime?.tasks || [], ed.agentTimings || {}, feed, ed.status).nodes
    .forEach(node => include(node));
  const represented = new Set([...progress.values()].map(entry => entry.meta.key));
  return {
    flat: flat.filter(event => ["orch", "harness"].includes(orchestrationAgentMeta(event.agent).role)),
    entries: [...visible.filter(entry => !["slide", "specialist"].includes(entry.type) || !represented.has(entry.meta.key)),
      ...progress.values()].sort((a, b) => a.order - b.order),
  };
}

function renderOrchestrationProgress(body, feed, {
  running = false,
  specialistArtifacts = ed.specialistArtifacts || {},
  scrollContainer = null,
  artifactMode = "inline",
} = {}) {
  if (!body) return;
  body.classList.add("is-orchestration");
  body.classList.toggle("artifact-links-mode", artifactMode === "links");
  body.closest(".outline-agent-turn")?.classList.add("is-orchestration");
  const { flat, entries } = orchestrationMainView(feed, specialistArtifacts);
  const scroller = scrollContainer || body.closest(".outline-chat-body") || body;
  const wasNearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
  const previousTop = scroller.scrollTop;
  const flowSignature = JSON.stringify(entries.map((entry) => [
    entry.key, entry.order, entry.completed,
    entry.eventTime?.text || "",
    entry.updates?.at(-1)?.text || entry.result || entry.text || entry.action || "",
  ]));
  const previousFlowSignature = body.dataset.orchestrationFlowSignature;
  const flowChanged = previousFlowSignature !== flowSignature;
  const firstFlowRender = !previousFlowSignature;
  const firstPopulatedFlow = entries.length > 0
    && (!previousFlowSignature || previousFlowSignature === "[]");
  const firstPrimaryLoad = (firstFlowRender || firstPopulatedFlow)
    && ["agent-progress-console", "workflow-agent-progress-console"].includes(body.id);
  const latest = [...flat].reverse().find((event) => runtimeEventState(event) || ["tool", "tool_result"].includes(event.k) || cleanAgentText(event.s));
  const latestMeta = orchestrationAgentMeta(latest?.agent || "orch");
  const runtimeState = runtimeEventState(latest);
  let stateTitle = running ? "任务启动准备中" : "整套任务记录已整理";
  let stateDetail = running ? "尚未收到 Agent 启动记录，不能确认模型已经开始响应。" : "可以在这里回看从规划、任务安排到整套检查的完整过程。";
  if (ed.status === "not_started") {
    stateTitle = "尚未开始生成";
    stateDetail = "这条 Query 已进入正式待生成清单，开始运行后会在这里展示完整制作过程。";
  } else if (ed.status === "completed") {
    const pageCount = Number(ed.total || ed.rendered?.size || 0);
    stateTitle = "整套演示已完成";
    stateDetail = pageCount
      ? `全部 ${pageCount} 页已经完成制作与检查，可以继续预览、播放或导出。`
      : "页面制作与检查已经完成，可以继续预览、播放或导出。";
    if (ed.trainingAdmission === "rejected") {
      stateTitle = "交付完成 · 训练样本未准入";
      stateDetail = "成品可以预览、下载；未计入训练正样本，不属于通道故障。原因："
        + (ed.trainingRejectionReasons.join("；") || "请查看训练准入记录。");
    }
  } else if (["failed", "rejected", "error", "paused_recoverable"].includes(ed.status)) {
    stateTitle = "本次生成未能完成";
    stateDetail = "已保留本轮制作记录，可查看具体阶段与失败原因。";
  } else if (["stopped", "interrupted"].includes(ed.status)) {
    stateTitle = "本次生成已中断";
    stateDetail = "已保留中断前的制作记录，可以回看进展或重新生成。";
  } else if (running && runtimeState) {
    stateTitle = `${latestMeta.label} · ${runtimeState[0]}`;
    stateDetail = runtimeState[1];
  } else if (running && latest?.k === "tool") {
    const [action, detail] = toolProgress(latest);
    stateTitle = `${latestMeta.label} · ${action}`;
    stateDetail = detail;
  } else if (running && latest?.k === "tool_result") {
    const label = TOOL_LABEL[latest.tool] || latest.tool || "工具事务";
    stateTitle = `${latestMeta.label} · ${label}${latest.ok ? "已完成" : "失败"}`;
    stateDetail = latest.ok
      ? `结果已返回${latest.duration_ms ? `，耗时 ${latest.duration_ms} ms` : ""}，正在继续下一步。`
      : `错误状态：${latest.error_code || "执行失败"}。`;
  } else if (running && latest) {
    const text = cleanAgentText(latest.s);
    stateTitle = `${latestMeta.label} 正在反馈`;
    stateDetail = compactOrchestrationCurrentDetail(latestMeta, text);
  }
  if (body.dataset.progressShell !== "orchestration" || !body.querySelector(".orchestration-stream")) {
    const configHtml = ["agent-progress-console", "workflow-agent-progress-console"].includes(body.id)
      ? outlineTaskConfigMarkup(editorPresentationKind(), taskConfigData())
      : "";
    body.dataset.progressShell = "orchestration";
    body.innerHTML = `
      <section class="agent-now orchestration-now"><div><span class="agent-kicker">当前工作</span><strong></strong><p></p></div></section>
      ${configHtml}
      <div class="orchestration-timing"></div>
      <div class="orchestration-overview"><span><b data-orch-count>${ed.status === "not_started" ? 0 : 1}</b> 整体策划</span><span class="research"><b data-research-count>0</b> Research</span><span class="image"><b data-image-count>0</b> Image</span><span class="material" hidden><b data-material-count>0</b> Material</span><span><b data-slide-count>0</b> 页面制作</span></div>
      <section class="orchestration-stream">
        <button class="orchestration-stream-head" type="button" aria-expanded="true">
          <span>编排与页面协作</span><em>Orchestrator 原始消息与工具 · 协作任务进展</em><i aria-hidden="true"></i>
        </button>
        <div class="orchestration-stream-body"><div class="orchestration-entry-list"></div></div>
      </section>
      <details class="agent-actions"><summary><span>底层执行细节</span><em></em><i></i></summary><div class="agent-action-list"></div></details>`;
  }
  const stream = body.querySelector(".orchestration-stream");
  const streamToggle = stream?.querySelector(".orchestration-stream-head");
  if (streamToggle && !streamToggle.dataset.bound) {
    streamToggle.dataset.bound = "1";
    streamToggle.addEventListener("click", () => {
      const collapsed = stream.classList.toggle("collapsed");
      streamToggle.setAttribute("aria-expanded", String(!collapsed));
    });
  }
  const now = body.querySelector(".agent-now");
  now.classList.toggle("is-running", running);
  now.classList.toggle("is-completed", ed.status === "completed");
  const nowKicker = now.querySelector(".agent-kicker");
  if (nowKicker) {
    nowKicker.textContent = ed.status === "completed"
      ? "任务完成"
      : (["failed", "rejected", "error", "stopped", "interrupted", "paused_recoverable"].includes(ed.status) ? "任务状态" : "当前工作");
  }
  const nowIcon = now.querySelector(".agent-now-icon");
  const nowGlyph = now.querySelector("[data-now-agent-glyph]");
  const glyphKey = `${editorPresentationKind()}:${latestMeta.role}:${latestMeta.key}`;
  if (nowGlyph && nowGlyph.dataset.glyphKey !== glyphKey) {
    nowGlyph.dataset.glyphKey = glyphKey;
    nowGlyph.innerHTML = orchestrationAgentGlyph(latestMeta, editorPresentationKind(), "orch-agent-glyph");
  }
  if (nowIcon) nowIcon.className = `agent-now-icon role-${latestMeta.role}`;
  now.querySelector("strong").textContent = stateTitle;
  now.querySelector("p").textContent = stateDetail;
  const timingNode = body.querySelector(".orchestration-timing");
  const timingMarkup = orchestrationTimingMarkup();
  if (timingNode.dataset.renderKey !== timingMarkup) {
    timingNode.dataset.renderKey = timingMarkup;
    timingNode.innerHTML = timingMarkup;
    timingNode.hidden = !timingMarkup;
  }
  const keys = Object.keys(feed || {});
  const slideCount = keys.filter((key) => orchestrationAgentMeta(key).role === "slide").length;
  const roleCount = (role) => keys.filter((key) => orchestrationAgentMeta(key).role === role).length;
  body.querySelector("[data-orch-count]").textContent = roleCount("orch") || "未启动";
  body.querySelector("[data-slide-count]").textContent = slideCount || "未启动";
  body.querySelector("[data-research-count]").textContent = roleCount("research") || "未启动";
  body.querySelector("[data-image-count]").textContent = roleCount("image") || "未启动";
  const materialCount = keys.filter((key) => orchestrationAgentMeta(key).role === "material" && feed[key]?.length).length;
  body.querySelector("[data-material-count]").textContent = materialCount;
  body.querySelector("[data-material-count]").closest("span").hidden = materialCount === 0;
  const list = body.querySelector(".orchestration-entry-list");
  reconcileOrchestrationEntries(list, entries, { artifactMode });
  if (ed.kind === "static" && body.id === "agent-progress-console") bindWorkflowTraceControls();
  if (ed.kind === "static" && ["agent-progress-console", "workflow-agent-progress-console"].includes(body.id)) {
    let making = body.querySelector(".mural-making-host");
    if (!making) {
      making = document.createElement("div");
      making.className = "mural-making-host";
      body.querySelector(".orchestration-stream").before(making);
    }
    const view = muralMakingView();
    const signature = JSON.stringify(view);
    if (making.dataset.renderKey !== signature) {
      const open = making.querySelector("details")?.open ?? true;
      making.innerHTML = muralMakingMarkup(view, view.active && body.id === "agent-progress-console");
      making.querySelector("details").open = open;
      making.dataset.renderKey = signature;
    }
  }
  body.dataset.orchestrationFlowSignature = flowSignature;
  const actions = summarizeToolEvents(flat);
  const transactions = liveToolTransactions(flat);
  const actionCount = flat.filter((event) => event.k === "tool").length;
  const failedTransactions = transactions.filter((item) => item.result && !item.result.ok).length;
  const openTransactions = transactions.filter((item) => !item.result).length;
  body.querySelector(".agent-actions summary em").textContent = actionCount
    ? `${actionCount} 次事务${openTransactions ? ` · ${openTransactions} 运行中` : ""}${failedTransactions ? ` · ${failedTransactions} 失败` : ""}`
    : "等待执行";
  const actionList = body.querySelector(".agent-action-list");
  const actionKey = JSON.stringify([running, actions, transactions]);
  if (actionList.dataset.renderKey !== actionKey) {
    const openToolIds = new Set($$(".live-transaction[open]", actionList).map((item) => item.dataset.toolUseId));
    actionList.dataset.renderKey = actionKey;
    actionList.innerHTML = transactions.length
      ? transactions.map((transaction) => liveToolTransactionMarkup(transaction, { showAgent: true, open: openToolIds.has(transaction.id), active: running })).join("")
      : actions.length ? actions.map((action, index) => `<div class="agent-action${index === 0 && running ? " active" : ""}"><span>${index === 0 && running ? "•" : "✓"}</span><b>${escapeHtml(action.label)}</b>${action.hint ? `<i>${escapeHtml(action.hint)}</i>` : ""}${action.count > 1 ? `<em>×${action.count}</em>` : ""}</div>`).join("") : '<div class="agent-action-empty">还没有底层执行记录</div>';
  }
  if (flowChanged) {
    requestAnimationFrame(() => {
      const shouldFollow = wasNearBottom || firstPrimaryLoad;
      const shouldJump = firstPrimaryLoad || (firstFlowRender && wasNearBottom);
      if (shouldFollow && shouldJump) {
        setScrollBottomImmediately(scroller);
        return;
      }
      scroller.scrollTo({
        top: shouldFollow ? scroller.scrollHeight : previousTop,
        behavior: shouldFollow && !firstFlowRender ? "smooth" : "auto",
      });
    });
  }
}

function miniRow(ev) {
  const d = document.createElement("div");
  d.className = "phl-row" + (["tool", "tool_result"].includes(ev.k) ? " cmd" : "");
  d.textContent = evLine(ev);
  return d;
}

function mergeIncrementalLiveFeed(current, delta, { reset = false } = {}) {
  const merged = reset ? {} : { ...(current || {}) };
  Object.entries(delta || {}).forEach(([agent, incoming]) => {
    let rows = reset ? [] : [...(merged[agent] || [])];
    const seen = new Set(rows.map(evKey));
    (incoming || []).forEach((event) => {
      const key = evKey(event);
      if (seen.has(key)) return;
      seen.add(key);
      if (event.partial) {
        const previous = rows.find((row) => row.stream_id === event.stream_id);
        const finalized = rows.some((row) => !row.partial && row.k === event.k
          && row.attempt_id && row.attempt_id === event.attempt_id);
        if (finalized || (previous && Number(previous.seq) >= Number(event.seq))) return;
        event = { ...event, stream_start_seq: previous?.stream_start_seq ?? previous?.seq ?? event.seq };
        rows = rows.filter((row) => row.stream_id !== event.stream_id);
      } else if (["text", "analysis"].includes(event.k)) {
        rows = rows.filter((row) => !(row.partial && row.k === event.k
          && row.segment_id === event.segment_id && row.turn === event.turn
          && (!event.attempt_id || row.attempt_id === event.attempt_id)));
      }
      if ((event.k === "provider" && event.phase === "failed")
          || (event.k === "status" && event.status === "failed")) {
        rows = rows.map((row) => row.partial
          && (event.attempt_id ? row.attempt_id === event.attempt_id : row.segment_id === event.segment_id)
          ? { ...row, phase: "interrupted" } : row);
      }
      rows.push(event);
    });
    rows.sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
    merged[agent] = rows.slice(-600);
    const context = [...rows].reverse().find((row) => row.k === "context");
    if (context && !merged[agent].includes(context)) merged[agent].unshift(context);
  });
  return merged;
}

async function fetchLive() {
  if (!ed.id) return;
  const deckId = ed.id;
  if (ed.liveFeedRequest?.deckId === deckId) return;
  const request = { deckId };
  ed.liveFeedRequest = request;
  const cursor = Number(ed.liveEventCursor || 0);
  let hasMore = false;
  try {
    const suffix = `/livefeed?cursor=${encodeURIComponent(cursor)}`;
    const payload = await jget(trajectoryMode ? trajectoryDeckApi(deckId, suffix) : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${deckId}${suffix}`);
    if (ed.id !== deckId || ed.liveFeedRequest !== request) return;
    if (payload.live_event_schema === "mural.live-event.v1") {
      const reset = payload.live_cursor_reset === true;
      ed.feed = mergeIncrementalLiveFeed(ed.feed, payload.agents || {}, { reset });
      ed.liveEventSchema = payload.live_event_schema;
      ed.liveEventCursor = Number(payload.live_next_cursor || 0);
    } else {
      ed.feed = payload.agents || {};
      ed.liveEventSchema = "";
      ed.liveEventCursor = 0;
    }
    ed.pageAgents = { ...(payload.page_agents || {}) };
    ed.specialistArtifacts = payload.specialist_artifacts || {};
    ed.agentTimings = payload.agent_timings || {};
    ed.overallTiming = payload.overall_timing || null;
    ed.workflow = payload.workflow || {};
    ed.liveFeedLoaded = true;
    hasMore = payload.live_has_more === true;
  }
  catch { return; }
  finally { if (ed.liveFeedRequest === request) ed.liveFeedRequest = null; }
  renderLive();
  if (ed.workspaceView === "workflow") renderWorkflowCanvas();
  if (ed.kind === "static" && ed.sel > 0) loadPageHistory(ed.sel, { quiet: true });
  if (hasMore) setTimeout(() => { if (ed.id === deckId) fetchLive(); }, 0);
}

function renderLive() {
  const feed = ed.feed || {};
  // 1) 胶片条:00 帧单行;生成中占位帧 = 该页设计师的全量微型滚动日志
  $$("#filmstrip .frame .ph .ph-log").forEach((el) => {
    const n = +el.closest(".frame").dataset.n;
    ed.phLines[n] = ed.phLines[n] || [];
    syncStream(el, feed[agentKey(n)] || [], ed.phLines[n], miniRow, 30, ".phl-row");
  });
  // 2) 00=整套任务的全 Agent 编排;其他页=该页设计师,未分派时回退编排器
  const con = $("#agent-progress-console") || $("#gen-console");
  if (con) {
    if (ed.sel === 0) {
      if (ed.conKey !== "orchestration") {
        ed.conKey = "orchestration"; ed.conLines = [];
        delete con.dataset.progressSignature;
        const title = $("#term-title");
        if (title) title.textContent = "全程编排";
      }
      renderOrchestrationProgress(con, feed, { running: isStaticActiveStatus(ed.status) });
      return;
    }
    let key = agentKey(ed.sel);
    let evs = feed[key] || [];
    if (ed.conKey !== key) {
      ed.conKey = key; ed.conLines = [];
      delete con.dataset.progressSignature;
      const t = $("#term-title");
      if (t) t.textContent = "本页制作进展";
      const pageAgent = $("#ce-agent");
      if (pageAgent) pageAgent.textContent = pageAgentLabel(key, ed.sel);
    }
    const running = isStaticActiveStatus(ed.status);
    renderAgentProgress(con, evs, {
      running,
      rendered: ed.sel > 0 && ed.rendered.has(ed.sel),
      n: ed.sel || 0,
      source: key,
    });
    const task = $("#ce-task");
    if (task) {
      const lastTool = [...evs].reverse().find((e) => e.k === "tool");
      const txt = lastTool ? `· ${TOOL_LABEL[lastTool.tool] || lastTool.tool}` : "";
      if (task.textContent !== txt) task.textContent = txt;
    }
  }
}

function startLive() {
  if (ed.liveTimer) return;
  fetchLive();
  // The Harness persists model deltas continuously when streaming is enabled.
  // Poll in small display batches; canonical completed turns still replace
  // partial text atomically, so this never creates a second conversation.
  ed.liveTimer = setInterval(fetchLive, 750);
}
function stopLive() {
  if (ed.liveTimer) { clearInterval(ed.liveTimer); ed.liveTimer = null; }
}

function updateNav() {
  // 注意 ed.sel===0(过程帧)是合法值,不能用 truthy 判断
  $("#canvas-pos").textContent = ed.sel != null ? `${pad2(ed.sel)} / ${ed.total ? pad2(ed.total) : "–"}` : "– / –";
  $("#prev-btn").disabled = ed.sel == null || ed.sel <= 1;
  $("#next-btn").disabled = ed.sel == null || (ed.total && ed.sel >= ed.total);
}

function setFollow(on) {
  ed.follow = on;
  $("#follow-btn").classList.toggle("on", on);
  if (on && ed.rendered.size) select(Math.max(...ed.rendered), { byUser: false });
}

/* ----- 本页规划 + 相关素材(画布上方) ----- */
async function loadPlan(n) {
  const panel = $("#plan-panel");
  if (ed.kind === "dynamic") { panel.hidden = true; return; }
  const cached = ed.planCache[n];
  if (cached === false) {                                  // 已知没有该页 md
    if (n === 0) panel.hidden = true; else renderPlanMissing(n);
    return;
  }
  if (cached) { renderPlan(n, cached); return; }
  let info;
  try { info = await jget(`${trajectoryMode ? trajectoryDeckApi(ed.id, "/slideinfo") : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/slideinfo`}?n=${n}`); }
  catch { panel.hidden = true; return; }
  if (ed.sel !== n) return;                                // 用户已切到别页
  if (!info.exists) {
    // 生成中规划可能还没写出来 → 不缓存为永久失败,稍后再试
    if (["completed", "failed", "rejected", "interrupted", "paused_recoverable"].includes(ed.status)) ed.planCache[n] = false;
    if (n === 0) panel.hidden = true;
    else renderPlanMissing(n);                             // 如实告知 + 指路全局大纲
    return;
  }
  ed.planCache[n] = info;
  renderPlan(n, info);
}

function renderPlanMissing(n) {
  const panel = $("#plan-panel");
  panel.hidden = false;
  panel.classList.remove("has-assets");
  panel.classList.toggle("collapsed", !!ed.ppCollapsed);
  $("#pp-title").textContent = `第 ${pad2(n)} 页 · 规划`;
  $("#pp-html").hidden = true;
  $("#pp-assets").hidden = true;
  $("#pp-md").innerHTML = `<p class="pp-missing">本页没有独立的规划文档(该模型把页面规划写在全局大纲里)。
    <button class="link" id="pp-goto">查看 00 · 全局大纲 →</button></p>`;
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
  $("#pp-goto").onclick = () => select(0, { byUser: true });
}

function renderPlan(n, info) {
  const panel = $("#plan-panel");
  panel.hidden = false;
  $("#pp-title").textContent = n === 0 ? "全局大纲 · deck.md" : `第 ${pad2(n)} 页 · 规划`;
  const link = $("#pp-html");
  if (info.html_rel) {
    link.hidden = false;
    link.href = trajectoryMode ? trajectoryDeckApi(ed.id, `/files/${info.html_rel}`) : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/files/${info.html_rel}`;
    link.textContent = n === 0 ? "设计系统 css" : "页面源码";
  } else link.hidden = true;
  $("#pp-md").innerHTML = info.html;
  const assets = info.assets || [];
  panel.classList.toggle("has-assets", !!assets.length);
  $("#pp-assets").hidden = !assets.length;
  $("#pp-assets-grid").innerHTML = assets.map((a) =>
    `<img class="pp-asset" src="${trajectoryMode ? trajectoryDeckApi(ed.id, `/files/${a}`) : `${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${ed.id}/files/${a}`}" title="${escapeHtml(a)}">`).join("");
  $$(".pp-asset").forEach((img) =>
    (img.onclick = () => { $("#lb-img").src = img.src; $("#lightbox").hidden = false; }));
  panel.classList.toggle("collapsed", !!ed.ppCollapsed);
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
}

/* ----- SSE ----- */
function subscribe(id) {
  const es = new EventSource(`${globalThis.STUDIO_BASE_PATH || ""}/api/decks/${id}/events`);
  ed.sse = es;
  es.onmessage = (ev) => {
    if (ed.id !== id) { es.close(); return; }
    let p; try { p = JSON.parse(ev.data); } catch { return; }
    if (p && p.phase) apply(p);
    if (p && ["completed", "failed", "rejected", "interrupted", "paused_recoverable"].includes(p.status)) { es.close(); if (ed.sse === es) ed.sse = null; }
  };
  es.addEventListener("end", () => { es.close(); if (ed.sse === es) ed.sse = null; });
}

/* ---------------- 初始化(defer 脚本,DOM 已就绪) ---------------- */
if (!$("#editor")) {
  /* 登录/注册页也会加载本脚本,没有工作台元素,直接不初始化 */
} else {
showComposer();
if (trajectoryMode) {
  const label = $("#new-btn .new-btn-label");
  if (label) label.textContent = "返回生成工作台";
  $("#new-btn")?.removeAttribute("aria-keyshortcuts");
  if ($("#batch-open-btn")) $("#batch-open-btn").hidden = true;
}
initRailDisclosureAnimations();
$(".creation-mode").addEventListener("click", (e) => {
  const card = e.target.closest(".creation-mode-card[data-mode]");
  if (!card) return;
  setCreationMode(card.dataset.mode);
});
$("#refresh-suggestions").addEventListener("click", renderSuggestions);
$("#user-menu-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setUserMenu($("#user-menu").hidden);
});
$("#user-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-user-action]");
  if (!item) return;
  const action = item.dataset.userAction;
  setUserMenu(false);
  if (action === "auth") setAuthGate(true);
  else if (action === "new") closeEditor();
  else if (action === "batch") openBatchModal();
  else if (action === "model") setModelDialog(true);
  else if (action === "services") setServiceDialog(true);
  else if (action === "environment") setEnvironmentDialog(true);
  else if (action === "static" || action === "dynamic") {
    setCreationMode(action);
    closeEditor();
    $("#q").focus();
  } else if (action === "switch") {
    switchAccount();
  } else if (action === "logout") {
    $("#logout-form").requestSubmit();
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#user-menu") && !e.target.closest("#user-menu-trigger") && !e.target.closest("#font-panel")) setUserMenu(false);
  if (e.target.closest("[data-open-auth]")) setAuthGate(true);
});
$$('[data-auth-tab]').forEach((button) => button.addEventListener("click", () => setAuthTab(button.dataset.authTab)));
$("#auth-login-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuthForm(event.currentTarget, `${globalThis.STUDIO_BASE_PATH || ""}/api/auth/login`);
});
$("#auth-register-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuthForm(event.currentTarget, `${globalThis.STUDIO_BASE_PATH || ""}/api/auth/register`);
});
$("#auth-gate-close")?.addEventListener("click", () => setAuthGate(false));
$("#auth-gate")?.addEventListener("click", (event) => {
  if (event.target === $("#auth-gate")) setAuthGate(false);
});
const savedThinkingPreference = localStorage.getItem("studio_thinking");
setThinkingEnabled(
  savedThinkingPreference === null ? true : savedThinkingPreference === "1",
  { remember: false },
);
$("#thinking-toggle").addEventListener("click", () => setThinkingEnabled(!thinkingEnabled()));
$("#effort-sel")?.addEventListener("change", (event) => {
  const model = $("#model-sel")?.value;
  if (model) localStorage.setItem(`studio_effort_${model}`, event.target.value);
});
$("#batch-model")?.addEventListener("change", syncBatchEffortControl);
$("#batch-effort")?.addEventListener("change", (event) => {
  const model = $("#batch-model")?.value;
  if (model) localStorage.setItem(`studio_effort_${model}`, event.target.value);
});
$("#style-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setDeckLanguageMenu(false);
  setLengthMenu(false);
  setModelMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setStyleGallery($("#style-gallery").hidden);
});
$("#length-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setDeckLanguageMenu(false);
  setStyleGallery(false);
  setModelMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setLengthMenu($("#length-menu").hidden);
});
$("#model-trigger").addEventListener("click", async (e) => {
  e.stopPropagation();
  setDeckLanguageMenu(false);
  setStyleGallery(false);
  setLengthMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  if (!$("#model-sel").options.length) {
    setModelMenu(false);
    await setModelDialog(true);
    return;
  }
  setModelMenu($("#model-menu").hidden);
});
$("#skill-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setDeckLanguageMenu(false);
  setStyleGallery(false);
  setLengthMenu(false);
  setModelMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setSkillMenu($("#skill-menu").hidden);
});
$("#deck-language-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setStyleGallery(false);
  setLengthMenu(false);
  setModelMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setDeckLanguageMenu($("#deck-language-menu").hidden);
});
$("#deck-language-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-deck-language]");
  if (!item) return;
  syncDeckLanguagePicker(item.dataset.deckLanguage);
  setDeckLanguageMenu(false);
  $("#deck-language-trigger").focus();
});
$("#deck-language-sel").addEventListener("change", (e) => syncDeckLanguagePicker(e.target.value));
$(".deck-language-control").addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    setDeckLanguageMenu(false);
    $("#deck-language-trigger").focus();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    setDeckLanguageMenu(true);
    const items = $$("#deck-language-menu [data-deck-language]");
    const index = items.indexOf(document.activeElement);
    const direction = e.key === "ArrowDown" ? 1 : -1;
    const next = index < 0 ? (direction > 0 ? 0 : items.length - 1) : (index + direction + items.length) % items.length;
    items[next].focus();
  }
});
$("#model-menu").addEventListener("click", (e) => {
  const deleteButton = e.target.closest("[data-delete-model]");
  if (deleteButton) {
    e.stopPropagation();
    void deleteCustomModel(deleteButton.dataset.deleteModel, deleteButton);
    return;
  }
  if (e.target.closest("[data-configure-model]")) {
    setModelMenu(false);
    void setModelDialog(true);
    return;
  }
  const item = e.target.closest("[data-model]");
  if (!item || item.disabled) return;
  $("#model-sel").value = item.dataset.model;
  normalizeVersionSelection();
  setModelMenu(false);
});
$("#skill-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-skill]");
  if (!item || item.disabled) return;
  if (creationMode === "dynamic") {
    dynamicSkill = item.dataset.skill;
    localStorage.setItem("studio_dynamic_skill", dynamicSkill);
    syncSkillPicker();
    setSkillMenu(false);
    return;
  }
  $("#skill-sel").value = item.dataset.skill;
  normalizeVersionSelection();
  setSkillMenu(false);
});
$("#length-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-length]");
  if (!item) return;
  syncLengthPicker(item.dataset.length);
  localStorage.setItem("studio_length", $("#length-sel").value);
  setLengthMenu(false);
});
$("#style-gallery-close").addEventListener("click", () => setStyleGallery(false));
$("#style-gallery").addEventListener("click", (e) => {
  const card = e.target.closest(".style-card[data-style]");
  if (!card) return;
  syncStylePicker(card.dataset.style || "");
  localStorage.setItem("studio_style", $("#style-sel").value);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".deck-language-control")) setDeckLanguageMenu(false);
  if (!e.target.closest("#style-gallery") && !e.target.closest("#style-trigger")) setStyleGallery(false);
  if (!e.target.closest("#length-menu") && !e.target.closest("#length-trigger")) setLengthMenu(false);
  if (!e.target.closest("#model-menu") && !e.target.closest("#model-trigger")) setModelMenu(false);
  if (!e.target.closest("#skill-menu") && !e.target.closest("#skill-trigger")) setSkillMenu(false);
  if (!e.target.closest("#attach-menu") && !e.target.closest("#attach-btn")) setAttachMenu(false);
  if (!e.target.closest("#font-panel") && !e.target.closest("#font-trigger")) setFontPanel(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { setStyleGallery(false); setLengthMenu(false); setDeckLanguageMenu(false); setModelMenu(false); setSkillMenu(false); setAttachMenu(false); setFontPanel(false); setUserMenu(false); }
});
$("#suggestion-list").addEventListener("click", (e) => {
  const chip = e.target.closest(".suggestion-chip");
  if (!chip) return;
  const input = $("#q");
  input.value = chip.dataset.query || chip.textContent;
  resizePrimaryComposerInput(input);
  input.focus();
});
$("#send").addEventListener("click", send);
$("#q").addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send(); });
$("#q").addEventListener("input", (e) => resizePrimaryComposerInput(e.target));
resizePrimaryComposerInput();
$("#workspace-view-switch")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-workspace-view]");
  if (!button) return;
  setWorkspaceView(button.dataset.workspaceView);
});
$("#page-edit-open")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled=true;
  try {
    const deckId=ed.id, page=Number(ed.sel);
    await window.MuralPageEditor.open({page, deckId, trigger:button, onSaved:async result=>{
      if (ed.id===deckId) { await openDeck(deckId); select(page, {byUser:true}); setWorkspaceView("ppt"); }
      showStudioNotice(result.rendered
        ? "原页面 HTML 已保存，PNG 已更新。修改后的正式导出仍需重新验收。"
        : `HTML 已保存，但截图未更新：${result.error || "渲染失败"}。请重新打开页面修正后保存，旧截图不代表本次修改。`);
    }});
  } catch (error) { showStudioNotice(error.message || "编辑画布未能打开"); }
  finally { syncViewToggle(); }
});
window.addEventListener("resize", () => {
  if (ed.workspaceView !== "workflow") return;
  const stage = $("#workflow-stage");
  if (!stage) return;
  delete stage.dataset.workflowSignature;
  requestAnimationFrame(renderWorkflowCanvas);
});
$("#view-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (!b || b.disabled) return;
  setPageView(b.dataset.view);
});
// 版本选择:用户选择模型和 Skill;pipeline 由后端给每个模型的默认适配版本自动同步。
// Keep retired built-ins readable for historical decks in the backend registry,
// but remove them immediately from every new-task selector even before the
// Python service is next reloaded.
const RETIRED_BUILT_IN_MODELS = new Set(["sensenova-flash-lite-v39-3"]);
[$("#model-sel"), $("#batch-model")].filter(Boolean).forEach((select) => {
  [...select.options].forEach((option) => {
    if (RETIRED_BUILT_IN_MODELS.has(option.value)) option.remove();
  });
});
if (RETIRED_BUILT_IN_MODELS.has(localStorage.getItem("studio_model"))) {
  localStorage.removeItem("studio_model");
  localStorage.removeItem("studio_model_default");
}
const currentDefaultModel = $("#model-sel").value;
const savedModel = localStorage.getItem("studio_model");
const savedModelDefault = localStorage.getItem("studio_model_default");
if (savedModel && savedModelDefault === currentDefaultModel && $(`#model-sel option[value="${savedModel}"]`)) {
  $("#model-sel").value = savedModel;
}
localStorage.setItem("studio_model_default", currentDefaultModel);
const currentDefaultSkill = $("#skill-sel").value;
const savedSkill = localStorage.getItem("studio_skill");
const savedSkillDefault = localStorage.getItem("studio_skill_default");
if (savedSkill && savedSkillDefault === currentDefaultSkill && $(`#skill-sel option[value="${savedSkill}"]`)) {
  $("#skill-sel").value = savedSkill;
}
localStorage.setItem("studio_skill_default", currentDefaultSkill);
// 篇幅控件：一级菜单与原生成参数保持同一取值契约。
const savedLength = localStorage.getItem("studio_length");
if (savedLength && $(`#length-sel option[value="${savedLength}"]`)) $("#length-sel").value = savedLength;
$("#length-sel").addEventListener("change", () => {
  localStorage.setItem("studio_length", $("#length-sel").value);
  syncLengthPicker($("#length-sel").value);
});
syncLengthPicker($("#length-sel").value);
["theme", "style", "scheme"].forEach((k) => {   // 生成偏好:记忆选择
  const el = $(`#${k}-sel`); if (!el) return;
  const saved = localStorage.getItem("studio_" + k);
  if (saved && [...el.options].some((o) => o.value === saved)) el.value = saved;
  el.addEventListener("change", () => localStorage.setItem("studio_" + k, el.value));
});
syncStylePicker($("#style-sel").value);
function modelBackend() {
  const opt = $("#model-sel").selectedOptions && $("#model-sel").selectedOptions[0];
  return opt ? opt.dataset.backend : "";
}
function selectedModelOption() {
  return $("#model-sel").selectedOptions && $("#model-sel").selectedOptions[0];
}
function selectedPipelineOption() {
  return $("#pipeline-sel").selectedOptions && $("#pipeline-sel").selectedOptions[0];
}
function selectedSkillOption() {
  return $("#skill-sel").selectedOptions && $("#skill-sel").selectedOptions[0];
}
function pipelineSupports(opt, backend) {
  return !!opt && (opt.dataset.supports || "").split(",").includes(backend);
}
function updatePipelineDisplay() {
  const pipeOpt = selectedPipelineOption();
  const display = $("#pipeline-display");
  if (display) display.textContent = pipeOpt ? (PIPELINE_LABEL[pipeOpt.value] || pipeOpt.textContent.trim()) : "自动";
}
function normalizeVersionSelection({ remember } = { remember: true }) {
  const modelOpt = selectedModelOption();
  const backend = modelBackend();
  $$("#pipeline-sel option").forEach((o) => {
    const ok = !modelOpt || pipelineSupports(o, backend);
    o.disabled = !ok;
  });
  $$("#skill-sel option").forEach((o) => {
    if (o.value === "long-horizon") {
      o.disabled = true;
      o.hidden = true;
      return;
    }
    const linked = $(`#pipeline-sel option[value="${o.dataset.pipeline || ""}"]`);
    o.disabled = o.dataset.ready !== "1" || (!!modelOpt && !pipelineSupports(linked, backend));
  });
  if (selectedSkillOption()?.disabled) {
    const fallback = $$("#skill-sel option").find((o) => !o.disabled);
    if (fallback) $("#skill-sel").value = fallback.value;
  }
  const matchedPipeline = selectedSkillOption()?.dataset.pipeline || modelOpt?.dataset.defaultPipeline || "";
  if (matchedPipeline && $(`#pipeline-sel option[value="${matchedPipeline}"]`)) {
    $("#pipeline-sel").value = matchedPipeline;
  }
  updatePipelineDisplay();
  syncAttachZone();                  // 管线能力联动:附件上传区随所选管线显隐
  syncFontControl();
  syncModelPicker();
  renderSkillMenu();
  if (remember) {
    localStorage.setItem("studio_model", $("#model-sel").value);
    localStorage.setItem("studio_skill", $("#skill-sel").value);
  }
}
$("#model-sel").addEventListener("change", () => normalizeVersionSelection());
$("#skill-sel").addEventListener("change", () => normalizeVersionSelection());
normalizeVersionSelection({ remember: false });
renderModelMenu();
$("#model-dialog-close").addEventListener("click", () => setModelDialog(false));
$("#model-form-cancel").addEventListener("click", () => setModelDialog(false));
$("#model-modal").addEventListener("click", (event) => {
  if (event.target === $("#model-modal")) setModelDialog(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#model-modal").hidden) setModelDialog(false);
  if (event.key === "Escape" && !$("#service-modal").hidden) setServiceDialog(false);
  if (event.key === "Escape" && !$("#environment-modal").hidden) setEnvironmentDialog(false);
});

$("#service-dialog-close").addEventListener("click", () => setServiceDialog(false));
$("#service-form-cancel").addEventListener("click", () => setServiceDialog(false));
$("#service-modal").addEventListener("click", (event) => {
  if (event.target === $("#service-modal")) setServiceDialog(false);
});
$("#environment-dialog-close").addEventListener("click", () => setEnvironmentDialog(false));
$("#environment-check-close").addEventListener("click", () => setEnvironmentDialog(false));
$("#environment-check-run").addEventListener("click", runEnvironmentCheck);
$("#environment-modal").addEventListener("click", (event) => {
  if (event.target === $("#environment-modal")) setEnvironmentDialog(false);
});
["image", "search", "visual-critic"].forEach((name) => {
  $(`#service-${name}-enabled`).addEventListener("change", syncServiceCards);
});
$("#service-image-provider").addEventListener("change", () => syncImageProviderFields({ applyDefaults: true }));
$("#service-generation-reset").addEventListener("click", () => {
  $("#service-max-tokens").value = 40960;
  $("#service-streaming-enabled").checked = false;
  $("#service-static-max-turns").value = 240;
  $("#service-static-subagent-max-turns").value = 240;
  $("#service-dynamic-max-turns").value = 4096;
  if ([...$("#service-title-task-model").options].some((option) => option.value === "deployment-model")) {
    $("#service-title-task-model").value = "deployment-model";
  }
});
$("#service-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  const errorBox = $("#service-form-error");
  errorBox.hidden = true;
  submit.disabled = true;
  const originalText = submit.textContent;
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/settings/services`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_enabled: $("#service-image-enabled").checked,
        image_provider: $("#service-image-provider").value,
        image_base_url: $("#service-image-url").value.trim(),
        image_model: $("#service-image-model").value.trim(),
        image_api_key: $("#service-image-key").value || null,
        clear_image_api_key: $("#service-image-clear").checked,
        search_enabled: $("#service-search-enabled").checked,
        search_base_url: $("#service-search-url").value.trim(),
        search_api_key: $("#service-search-key").value || null,
        clear_search_api_key: $("#service-search-clear").checked,
        max_tokens: Number($("#service-max-tokens").value),
        streaming_enabled: $("#service-streaming-enabled").checked,
        enable_visual_critic: $("#service-visual-critic-enabled").checked,
        static_max_turns: Number($("#service-static-max-turns").value),
        static_subagent_max_turns: Number($("#service-static-subagent-max-turns").value),
        dynamic_max_turns: Number($("#service-dynamic-max-turns").value),
        title_task_model: $("#service-title-task-model").value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "服务配置保存失败");
    applyServiceSettings(payload);
    submit.textContent = "已保存";
    setTimeout(() => setServiceDialog(false), 360);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
    setTimeout(() => { submit.textContent = originalText; }, 500);
  }
});
$("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  const errorBox = $("#model-form-error");
  errorBox.hidden = true;
  submit.disabled = true;
  try {
    const response = await fetch(`${globalThis.STUDIO_BASE_PATH || ""}/api/models/custom`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#custom-model-name").value.trim(),
        model_id: $("#custom-model-id").value.trim(),
        base_url: $("#custom-model-url").value.trim(),
        api_key: $("#custom-model-key").value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "模型添加失败");
    customModelItems.push(payload);
    upsertCustomModelOption(payload, { select: true });
    renderCustomModelList();
    form.reset();
    await setModelDialog(false);
    void guideOptionalMaterialServices();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
$("#custom-model-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-model]");
  if (!button) return;
  await deleteCustomModel(button.dataset.deleteModel, button);
});
if ($("#attach-btn")) {              // 附件上传区交互(仅支持附件的管线可见)
  $("#attach-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    if (creationMode !== "static") return;
    setStyleGallery(false);
    setLengthMenu(false);
    setDeckLanguageMenu(false);
    setModelMenu(false);
    setSkillMenu(false);
    setAttachMenu($("#attach-menu").hidden);
  });
  $("#attach-upload-action").addEventListener("click", () => {
    if (creationMode !== "static") return;
    setAttachMenu(false);
    $("#attach-input").click();
  });
  $("#attach-input").addEventListener("change", (e) => {
    if (creationMode !== "static") {
      e.target.value = "";
      clearAttachFiles();
      return;
    }
    for (const f of e.target.files) {
      if (attachFiles.length >= 8) { alert("最多 8 个附件"); break; }
      if (f.size > 20 * 1024 * 1024) { alert(`「${f.name}」超过 20MB,已跳过`); continue; }
      attachFiles.push(f);
    }
    e.target.value = "";             // 允许再次选同名文件
    renderAttachList();
  });
  $("#attach-list").addEventListener("click", (e) => {
    const remove = e.target.closest(".attach-x");
    if (remove) {
      const [file] = attachFiles.splice(+remove.dataset.i, 1);
      if (file) releaseAttachmentObjectUrl(file);
      renderAttachList();
      return;
    }
    const open = e.target.closest("[data-local-attachment]");
    if (!open) return;
    const file = attachFiles[+open.dataset.localAttachment];
    if (!file) return;
    const url = attachmentObjectUrl(file);
    openAttachmentViewer({ name: file.name, type: file.type, size: file.size, url, downloadUrl: url });
  });
}
if ($("#font-trigger")) {
  renderFontControls();
  $("#font-trigger").addEventListener("click", (event) => {
    event.stopPropagation();
    if (creationMode !== "static" || !pipelineHasCap("custom_fonts")) return;
    setStyleGallery(false); setLengthMenu(false); setDeckLanguageMenu(false); setModelMenu(false); setSkillMenu(false); setAttachMenu(false);
    setFontPanel($("#font-panel").hidden);
  });
  $("#font-upload").addEventListener("click", () => $("#font-input").click());
  $("#font-input").addEventListener("change", (event) => {
    for (const file of event.target.files || []) {
      if (fontFiles.length >= 6) { alert("自定义字体最多上传 6 个"); break; }
      if (!/\.(ttf|otf|woff2?)$/i.test(file.name)) { alert(`「${file.name}」不是支持的字体格式`); continue; }
      if (file.size > 25 * 1024 * 1024) { alert(`「${file.name}」超过 25MB，已跳过`); continue; }
      if (fontFiles.some((item) => item.name === file.name && item.size === file.size)) continue;
      fontFiles.push(file);
    }
    event.target.value = "";
    renderFontControls();
  });
  $("#font-file-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-font-remove]");
    if (!button) return;
    fontFiles.splice(Number(button.dataset.fontRemove), 1);
    renderFontControls();
  });
  $("#font-panel").addEventListener("change", (event) => {
    if (event.target.matches("[data-font-role]")) renderFontControls();
  });
}
function startNewConversation() {
  if (trajectoryMode) { window.location.href = (globalThis.STUDIO_BASE_PATH || "") + "/"; return; }
  if (!$("#batch-modal").hidden) closeBatchModal();
  if (!$("#model-modal").hidden) setModelDialog(false);
  setUserMenu(false);
  closeEditor();
  requestAnimationFrame(() => $("#q")?.focus());
}
$("#new-btn").addEventListener("click", startNewConversation);
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    startNewConversation();
  }
});
const creationWorkspace = $("#creation-workspace");
let ambientPointerFrame = 0;
creationWorkspace?.addEventListener("pointermove", (event) => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  cancelAnimationFrame(ambientPointerFrame);
  ambientPointerFrame = requestAnimationFrame(() => {
    const bounds = creationWorkspace.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width - .5) * 20;
    const y = ((event.clientY - bounds.top) / bounds.height - .5) * 14;
    creationWorkspace.style.setProperty("--flow-x", `${x.toFixed(2)}px`);
    creationWorkspace.style.setProperty("--flow-y", `${y.toFixed(2)}px`);
  });
});
creationWorkspace?.addEventListener("pointerleave", () => {
  creationWorkspace.style.setProperty("--flow-x", "0px");
  creationWorkspace.style.setProperty("--flow-y", "0px");
});
$("#sidebar-toggle").addEventListener("click", () => {
  setSidebarCollapsed(!document.querySelector(".layout")?.classList.contains("sidebar-collapsed"));
});
$("#sidebar-expand").addEventListener("click", () => setSidebarCollapsed(false));
$("#sidebar-scrim")?.addEventListener("click", () => setSidebarCollapsed(true, { remember: false }));
$("#proc-toggle")?.addEventListener("click", () => {
  if (matchMedia("(max-width: 1799px)").matches) {
    setProcRailOpen(!$("#editor")?.classList.contains("proc-open"));
    return;
  }
  setProcRailCollapsed(!$("#editor")?.classList.contains("proc-rail-collapsed"));
});
$("#proc-rail-collapse")?.addEventListener("click", () => {
  if (matchMedia("(max-width: 1799px)").matches) setProcRailOpen(false);
  else setProcRailCollapsed(true);
});
$("#proc-rail-expand")?.addEventListener("click", () => setProcRailCollapsed(false));
$("#proc-scrim")?.addEventListener("click", () => setProcRailOpen(false));
$("#canvas-deck").addEventListener("load", bindDynamicDeckNavigation);
if (typeof ResizeObserver === "function") {
  const deckViewportObserver = new ResizeObserver(() => scheduleDeckViewportFit());
  deckViewportObserver.observe($("#canvas").parentElement);
  deckViewportObserver.observe($(".canvas-bottom-dock"));
}
window.addEventListener("resize", () => {
  scheduleDeckViewportFit();
  if (!$("#composer")?.hidden) resizePrimaryComposerInput();
});
function syncPresentationFullscreenScale() {
  const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
  const presentationFullscreen = fullscreenElement === $("#canvas");
  document.documentElement.classList.toggle("presentation-fullscreen", presentationFullscreen);
  scheduleDeckViewportFit();
  setTimeout(() => scheduleDeckViewportFit(), 180);
}
document.addEventListener("fullscreenchange", syncPresentationFullscreenScale);
document.addEventListener("webkitfullscreenchange", syncPresentationFullscreenScale);
$$('[data-theme-choice]').forEach((choice) => choice.addEventListener("click", () => {
  setTheme(choice.dataset.themeChoice);
}));
$("#settings-language").addEventListener("change", (event) => {
  setLanguage(event.target.value);
});
$("#batch-open-btn").addEventListener("click", openBatchModal);
$("#batch-close").addEventListener("click", closeBatchModal);
$("#batch-refresh").addEventListener("click", () => loadBatches());
$("#batch-form").addEventListener("submit", submitBatch);
$("#batch-modal").addEventListener("click", (e) => {
  if (e.target.id === "batch-modal") closeBatchModal();
});
$("#back-btn").addEventListener("click", () => {
  if (trajectoryMode) { window.location.href = (globalThis.STUDIO_BASE_PATH || "") + "/"; return; }
  closeEditor(); loadDecks();
});
$("#prev-btn").addEventListener("click", () => ed.sel > 1 && select(ed.sel - 1, { byUser: true }));
$("#next-btn").addEventListener("click", () => ed.sel != null && ed.sel < ed.total && select(ed.sel + 1, { byUser: true }));
$("#follow-btn").addEventListener("click", () => setFollow(!ed.follow));
$("#page-speech-toggle").addEventListener("click", () => {
  const drawer = $("#pr-speech-section");
  const collapsed = !drawer.classList.contains("collapsed");
  localStorage.setItem("studio_speech_collapsed", collapsed ? "1" : "0");
  drawer.classList.toggle("collapsed", collapsed);
  $("#page-speech-toggle").setAttribute("aria-expanded", String(!collapsed));
  requestAnimationFrame(() => scheduleDeckViewportFit());
  setTimeout(() => scheduleDeckViewportFit(), 440);
});
$("#page-speech-source-toggle").addEventListener("click", () => {
  const drawer = $("#pr-speech-section");
  const toggle = $("#page-speech-source-toggle");
  const evidence = $("#page-speech-evidence");
  if (!evidence) return;
  if (drawer.classList.contains("collapsed")) {
    drawer.classList.remove("collapsed");
    localStorage.setItem("studio_speech_collapsed", "0");
    $("#page-speech-toggle").setAttribute("aria-expanded", "true");
  }
  const open = toggle.getAttribute("aria-expanded") !== "true";
  toggle.setAttribute("aria-expanded", String(open));
  evidence.hidden = !open;
});
document.addEventListener("keydown", (e) => {
  if ($("#editor").hidden || e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (e.key === "ArrowLeft" && ed.sel > 1) select(ed.sel - 1, { byUser: true });
  if (e.key === "ArrowRight" && ed.sel != null && ed.sel < ed.total) select(ed.sel + 1, { byUser: true });
});
$("#canvas").addEventListener("click", (e) => {
  if (e.target.id === "canvas-img") { $("#lb-img").src = e.target.src; $("#lightbox").hidden = false; }
});
$("#pr-feed").addEventListener("click", (e) => {
  if (e.target.classList.contains("pr-thumb") || e.target.closest(".pr-history-shot")) {
    const image = e.target.closest(".pr-history-shot")?.querySelector("img") || e.target;
    $("#lb-img").src = image.src; $("#lightbox").hidden = false;
  }
});
$("#pr-page-history").addEventListener("click", (e) => {
  const shot = e.target.closest(".pr-history-shot");
  if (!shot) return;
  const image = shot.querySelector("img");
  if (image) { $("#lb-img").src = image.src; $("#lightbox").hidden = false; }
});
$("#pp-toggle").addEventListener("click", () => {
  ed.ppCollapsed = !ed.ppCollapsed;
  $("#plan-panel").classList.toggle("collapsed", ed.ppCollapsed);
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeHistoryContextMenu();
    closeHistoryRenameDialog();
    closeExportMenu();
    $("#lightbox").hidden = true;
    closeAttachmentViewer();
    closeOverviewModal();
    closeBatchModal();
    setAuthGate(false);
  }
});
$("#lightbox").addEventListener("click", () => { $("#lightbox").hidden = true; });
$("#attachment-viewer-close").addEventListener("click", closeAttachmentViewer);
$("#attachment-viewer").addEventListener("click", (event) => {
  if (event.target === $("#attachment-viewer")) closeAttachmentViewer();
});
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-attachment-preview]");
  if (!trigger || trigger.disabled) return;
  const url = trigger.dataset.attachmentUrl || "";
  const fallbackUrl = trigger.dataset.attachmentFallbackUrl || "";
  openAttachmentViewer({
    name: trigger.dataset.attachmentName || "附件",
    type: trigger.dataset.attachmentType || "",
    size: Number(trigger.dataset.attachmentSize) || 0,
    url,
    fallbackUrl,
    previewUrl: trigger.dataset.attachmentPreviewUrl || "",
    downloadUrl: `${url}${url.includes("?") ? "&" : "?"}download=1`,
  });
});
document.addEventListener("error", (event) => {
  const image = event.target?.closest?.("img[data-attachment-image-fallback]");
  if (!image) return;
  const fallback = image.dataset.attachmentImageFallback || "";
  delete image.dataset.attachmentImageFallback;
  if (fallback && image.src !== new URL(fallback, location.origin).href) image.src = fallback;
}, true);
$("#overview-close").addEventListener("click", closeOverviewModal);
$("#overview-modal").addEventListener("click", (e) => {
  if (e.target.id === "overview-modal") closeOverviewModal();
});
$("#overview-grid").addEventListener("click", (e) => {
  const card = e.target.closest(".overview-card");
  if (!card || card.disabled) return;
  closeOverviewModal();
  ed.workspaceView = "ppt";
  ed.viewMode = "ppt";
  localStorage.setItem("studio_page_view", ed.viewMode);
  syncViewToggle();
  select(+card.dataset.n, { byUser: true });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#history-context-menu")) closeHistoryContextMenu();
  if (!event.target.closest(".export-menu-wrap")) closeExportMenu();
  const button = event.target.closest(".outline-query-copy");
  if (button) copyOutlineQuery(button);
});
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (details.matches?.(".live-transaction") && details.open) loadLiveToolDetails(details);
}, true);
const compactWorkspaceMedia = matchMedia("(max-width: 1180px)");
const procRailDrawerMedia = matchMedia("(max-width: 1799px)");
const syncResponsiveSidebar = (media = compactWorkspaceMedia) => {
  const collapsed = media.matches || localStorage.getItem("studio_sidebar_collapsed") === "1";
  setSidebarCollapsed(collapsed, { remember: false });
};
const syncResponsiveProcRail = (media = procRailDrawerMedia) => {
  setProcRailOpen(false);
  const collapsed = !media.matches && localStorage.getItem("studio_proc_rail_collapsed") === "1";
  setProcRailCollapsed(collapsed, { remember: false });
};
syncResponsiveSidebar();
syncResponsiveProcRail();
if (compactWorkspaceMedia.addEventListener) compactWorkspaceMedia.addEventListener("change", syncResponsiveSidebar);
else compactWorkspaceMedia.addListener(syncResponsiveSidebar);
if (procRailDrawerMedia.addEventListener) procRailDrawerMedia.addEventListener("change", syncResponsiveProcRail);
else procRailDrawerMedia.addListener(syncResponsiveProcRail);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !compactWorkspaceMedia.matches) return;
  setProcRailOpen(false);
  if (!document.querySelector(".layout")?.classList.contains("sidebar-collapsed")) {
    setSidebarCollapsed(true, { remember: false });
  }
});
setTheme(document.documentElement.dataset.theme || "dark", { remember: false });
setLanguage(currentLanguage, { remember: false });
setTopbar();
initHistorySearch();
const initialParams = new URLSearchParams(location.search);
if (initialParams.get("mode") === "dynamic") setCreationMode("dynamic", { remember: false });
if (["login", "register"].includes(initialParams.get("auth"))) {
  setAuthGate(true, { tab: initialParams.get("auth") });
}
loadDecks().then(openDeckFromLocationHash);
}
