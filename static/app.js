// ============ 共用工具 ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `GET ${path} failed (${res.status})`);
  }
  return res.json();
}

async function apiPost(path, data) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `POST ${path} failed (${res.status})`);
  }
  return res.json();
}

// ============ YouTube 表情符號引擎 ============
let youtubeEmojiMap = {};
async function initEmojis() {
  try {
    youtubeEmojiMap = await apiGet("/api/emojis");
  } catch (err) {
    console.warn("無法載入表情對照表:", err);
  }
}
initEmojis();

function renderEmojiHtml(text) {
  if (!text) return "";
  const safeText = escapeHtml(text);
  // 比對 :label: 格式
  return safeText.replace(/:([a-zA-Z0-9_\-]+):/g, (match) => {
    const url = youtubeEmojiMap[match];
    if (url) {
      return `<img class="yt-emoji" src="${url}" alt="${match}" title="${match}" loading="lazy">`;
    }
    return `<span class="yt-custom-emote">${match}</span>`;
  });
}

function formatSeconds(secs) {
  if (secs == null || isNaN(secs)) return "";
  const total = Math.floor(secs);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

// ============ 錯誤提示 Toast ============
let toastTimer = null;
function showError(message) {
  const toast = $("#error-toast");
  $("#error-toast-text").textContent = message;
  toast.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 6000);
}
$("#error-toast-close").addEventListener("click", () => $("#error-toast").classList.add("hidden"));

// ============ Step 切換 ============
function goToStep(stepId) {
  $$(".step").forEach((s) => s.classList.remove("active"));
  $(`#${stepId}`).classList.add("active");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ============ 全域狀態 ============
const state = {
  selectedChannelDbId: null,
  selectedChannelName: null,
  quizzes: [],         // 這一輪的所有題目
  currentIndex: 0,      // 目前作答到第幾題
  answers: [],           // 每題的作答結果 ("correct" | "wrong" | null)
  quizHistory: [],       // 每一題的詳細作答紀錄（含題目、選項、選擇與正誤）
};

// ============ 進階選項摺疊 ============
$("#advanced-toggle").addEventListener("click", () => {
  const panel = $("#advanced-panel");
  panel.classList.toggle("hidden");
  $("#advanced-toggle").textContent = panel.classList.contains("hidden") ? "進階選項 ▾" : "進階選項 ▴";
});

// ============ 除錯區塊摺疊 ============
$("#debug-toggle").addEventListener("click", () => {
  const panel = $("#debug-panel");
  panel.classList.toggle("hidden");
  $("#debug-toggle").textContent = panel.classList.contains("hidden") ? "除錯 / 資料檢視 ▾" : "除錯 / 資料檢視 ▴";
  if (!panel.classList.contains("hidden")) {
    refreshDebugChannelSelects();
  }
});

// ============ STEP 1: 抓取頻道 ============
const STAGE_LABELS = {
  resolve_channel: "解析頻道中...",
  list_streams: "列出過往直播中...",
  fetch_chat: "抓取聊天室訊息中...",
  filter: "清洗過濾留言中...",
  resolve_names: "補完觀眾暱稱中...",
  analyze: "分析觀眾風格中...",
  similarity: "計算觀眾相似度中...",
  done: "完成！",
};
const STAGE_ORDER = ["resolve_channel", "list_streams", "fetch_chat", "filter", "resolve_names", "analyze", "similarity", "done"];

let pollTimer = null;
let lastJobRaw = null;

function setButtonProgress(percent, label, disabled = true) {
  const btn = $("#start-fetch-btn");
  const fill = $("#start-fetch-btn-fill");
  const text = $("#start-fetch-btn-text");

  btn.disabled = disabled;
  if (percent > 0) {
    fill.style.width = Math.min(100, Math.max(0, percent)) + "%";
  } else {
    fill.style.width = "0%";
  }
  if (label) {
    text.textContent = label;
  }
}

$("#start-fetch-btn").addEventListener("click", async () => {
  const channel = $("#channel-input").value.trim();
  if (!channel) {
    showError("請輸入頻道網址、@handle 或 channel id");
    return;
  }
  const maxStreams = $("#max-streams").value ? parseInt($("#max-streams").value, 10) : null;
  const nameLimit = $("#name-limit").value ? parseInt($("#name-limit").value, 10) : null;
  const maxConcurrent = $("#max-concurrent").value ? parseInt($("#max-concurrent").value, 10) : 3;

  setButtonProgress(5, "送出請求中...", true);
  $("#job-status").classList.remove("hidden");
  $("#job-stage").textContent = "送出請求中...";
  $("#progress-fill").style.width = "3%";
  $("#in-progress-container").classList.add("hidden");

  try {
    const { job_id } = await apiPost("/api/channels/fetch", {
      channel,
      max_streams: maxStreams,
      name_resolve_limit: nameLimit,
      max_concurrent_fetches: maxConcurrent,
    });
    pollJob(job_id);
  } catch (err) {
    setButtonProgress(0, "開始抓取", false);
    $("#job-status").classList.add("hidden");
    showError("送出抓取請求失敗：" + err.message);
  }
});

function pollJob(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await apiGet(`/api/jobs/${jobId}`);
      lastJobRaw = job;
      $("#debug-job-raw").textContent = JSON.stringify(job, null, 2);
      renderJobStatus(job);
      if (job.status === "done" || job.status === "error") {
        clearInterval(pollTimer);
        setButtonProgress(0, "開始抓取", false);
        if (job.status === "done") {
          refreshChannels();
        } else {
          showError("抓取過程發生錯誤，詳情請展開下方「除錯 / 資料檢視」查看。");
        }
      }
    } catch (err) {
      clearInterval(pollTimer);
      setButtonProgress(0, "開始抓取", false);
      showError("查詢進度失敗：" + err.message);
    }
  }, 1200);
}

function renderJobStatus(job) {
  const p = job.progress || {};
  let stageLabel = STAGE_LABELS[job.stage] || job.stage || "處理中...";

  const idx = STAGE_ORDER.indexOf(job.stage);
  let pct = idx >= 0 ? ((idx + 1) / STAGE_ORDER.length) * 100 : 5;

  // fetch_chat 階段採用並行進度計算
  if (job.stage === "fetch_chat" && p.total_streams) {
    const completed = p.completed_streams || 0;
    const failed = p.failed_streams || 0;
    const total = p.total_streams || 1;
    const processed = completed + failed;
    const streamRatio = total > 0 ? (processed / total) : 0;

    const basePct = (STAGE_ORDER.indexOf("fetch_chat") / STAGE_ORDER.length) * 100;
    const spanPct = (1 / STAGE_ORDER.length) * 100;
    pct = basePct + streamRatio * spanPct;

    stageLabel = `抓取聊天室訊息中 (已完成 ${completed} / ${total} 場${failed ? `，失敗 ${failed} 場` : ""})`;

    // 渲染同時處理中的場次標籤
    const inProgContainer = $("#in-progress-container");
    const inProgChips = $("#in-progress-chips");
    if (p.in_progress && p.in_progress.length > 0) {
      inProgContainer.classList.remove("hidden");
      inProgChips.innerHTML = p.in_progress
        .map((title) => `<span class="stream-chip">📺 ${escapeHtml(title)}</span>`)
        .join("");
    } else {
      inProgContainer.classList.add("hidden");
    }
  } else {
    $("#in-progress-container").classList.add("hidden");
  }

  if (job.status === "error") {
    $("#job-stage").textContent = "發生錯誤，請查看下方除錯區塊";
    setButtonProgress(0, "開始抓取", false);
  } else {
    $("#job-stage").textContent = stageLabel;
    const roundedPct = Math.round(pct);
    $("#progress-fill").style.width = Math.max(pct, 3) + "%";
    setButtonProgress(pct, `抓取中 ${roundedPct}% (${stageLabel})`, true);
  }
}

async function refreshChannels() {
  try {
    const channels = await apiGet("/api/channels");
    const list = $("#channel-list");
    list.innerHTML = "";
    if (channels.length === 0) {
      list.innerHTML = '<p class="empty-text">尚未抓取任何頻道</p>';
      return;
    }
    channels.forEach((c) => {
      const div = document.createElement("div");
      div.className = "channel-item";
      div.innerHTML = `
        <div>
          <div class="name">${escapeHtml(c.channel_name || c.channel_id)}</div>
          <div class="meta">已抓取 ${c.streams_fetched} / ${c.stream_count} 場直播</div>
        </div>
        <span class="link-btn small">選擇 →</span>
      `;
      div.addEventListener("click", () => selectChannel(c.channel_db_id, c.channel_name || c.channel_id));
      list.appendChild(div);
    });
  } catch (err) {
    showError("載入頻道清單失敗：" + err.message);
  }
}

$("#refresh-channels-btn").addEventListener("click", refreshChannels);

function selectChannel(channelDbId, channelName) {
  state.selectedChannelDbId = channelDbId;
  state.selectedChannelName = channelName;
  $("#selected-channel-name").textContent = channelName;
  goToStep("step-setup");
}

$("#change-channel-btn").addEventListener("click", () => goToStep("step-fetch"));

// ============ STEP 2: 出題設定（難度滑桿）============
function normalizeSliders(changedId) {
  const easy = $("#easy-ratio");
  const medium = $("#medium-ratio");
  const hard = $("#hard-ratio");

  let e = parseInt(easy.value, 10);
  let m = parseInt(medium.value, 10);
  let h = parseInt(hard.value, 10);
  const total = e + m + h;

  if (total !== 100 && total > 0) {
    const others = ["easy", "medium", "hard"].filter((k) => k !== changedId);
    const changedVal = { easy: e, medium: m, hard: h }[changedId];
    const remaining = 100 - changedVal;
    const otherSum = others.reduce((s, k) => s + { easy: e, medium: m, hard: h }[k], 0);

    if (otherSum > 0) {
      others.forEach((k) => {
        const cur = { easy: e, medium: m, hard: h }[k];
        const newVal = Math.round((cur / otherSum) * remaining);
        if (k === "easy") e = newVal;
        if (k === "medium") m = newVal;
        if (k === "hard") h = newVal;
      });
    } else {
      others.forEach((k, i) => {
        const val = Math.floor(remaining / others.length) + (i === 0 ? remaining % others.length : 0);
        if (k === "easy") e = val;
        if (k === "medium") m = val;
        if (k === "hard") h = val;
      });
    }
    const fixSum = e + m + h;
    const diff = 100 - fixSum;
    if (diff !== 0) {
      const target = others[0];
      if (target === "easy") e += diff;
      if (target === "medium") m += diff;
      if (target === "hard") h += diff;
    }
  }

  easy.value = e;
  medium.value = m;
  hard.value = h;
  $("#easy-value").textContent = e + "%";
  $("#medium-value").textContent = m + "%";
  $("#hard-value").textContent = h + "%";
}

["easy-ratio", "medium-ratio", "hard-ratio"].forEach((id) => {
  $(`#${id}`).addEventListener("input", () => {
    const key = id.replace("-ratio", "");
    normalizeSliders(key);
  });
});

$("#start-quiz-btn").addEventListener("click", async () => {
  if (!state.selectedChannelDbId) {
    showError("請先選擇頻道");
    return;
  }
  const totalCount = parseInt($("#question-count").value, 10) || 10;
  const easyRatio = parseInt($("#easy-ratio").value, 10) / 100;
  const mediumRatio = parseInt($("#medium-ratio").value, 10) / 100;
  const hardRatio = parseInt($("#hard-ratio").value, 10) / 100;

  $("#start-quiz-btn").disabled = true;
  try {
    const result = await apiPost(`/api/channels/${state.selectedChannelDbId}/quiz/batch`, {
      total_count: totalCount,
      easy_ratio: easyRatio,
      medium_ratio: mediumRatio,
      hard_ratio: hardRatio,
    });
    state.quizzes = result.quizzes;
    state.currentIndex = 0;
    state.answers = new Array(result.quizzes.length).fill(null);
    state.quizHistory = [];

    if (result.actual_count < result.requested_count) {
      showError(`資料量不足，只能產生 ${result.actual_count} / ${result.requested_count} 題`);
    }

    renderProgressDots();
    renderCurrentQuiz();
    goToStep("step-quiz");
  } catch (err) {
    showError("出題失敗：" + err.message);
  } finally {
    $("#start-quiz-btn").disabled = false;
  }
});

// ============ STEP 3: 作答 ============
const DIFFICULTY_LABELS = { easy: "簡單", medium: "普通", hard: "困難", random: "隨機" };

function renderProgressDots() {
  const container = $("#quiz-progress-dots");
  container.innerHTML = "";

  const total = state.quizzes.length;
  const current = state.currentIndex;

  const windowSize = 5;
  let start = Math.max(0, current - Math.floor(windowSize / 2));
  let end = Math.min(total, start + windowSize);
  start = Math.max(0, end - windowSize);

  for (let i = start; i < end; i++) {
    const dot = document.createElement("div");
    dot.className = "progress-dot";
    if (i === current) {
      dot.classList.add("current");
      dot.textContent = i + 1;
    } else if (state.answers[i] === "correct") {
      dot.classList.add("correct");
      dot.textContent = "✓";
    } else if (state.answers[i] === "wrong") {
      dot.classList.add("wrong");
      dot.textContent = "✕";
    } else {
      dot.classList.add("pending");
      dot.textContent = i + 1;
    }
    container.appendChild(dot);
  }
}

function renderCurrentQuiz() {
  const quiz = state.quizzes[state.currentIndex];
  if (!quiz) return;

  $("#quiz-stream-title").textContent = quiz.stream_title || "";

  const badge = $("#quiz-difficulty-badge");
  badge.className = "badge " + (quiz.difficulty_level || "");
  badge.textContent = DIFFICULTY_LABELS[quiz.difficulty_level] || "";

  // Super Chat 酷炫橫幅判斷
  const scBanner = $("#quiz-sc-banner");
  const quizCard = $("#quiz-card-container");
  if (quiz.amount || quiz.message_type === "paid_message" || quiz.message_type === "superchat") {
    scBanner.classList.remove("hidden");
    scBanner.innerHTML = `
      <span class="sc-sparkle">✨</span>
      <span class="sc-icon">💰</span>
      <span class="sc-title">SUPER CHAT 超級留言</span>
      <span class="sc-amount">${escapeHtml(quiz.amount || "贊助留言")}</span>
      <span class="sc-sparkle">✨</span>
    `;
    if (quizCard) quizCard.classList.add("superchat-glow");
  } else {
    scBanner.classList.add("hidden");
    scBanner.innerHTML = "";
    if (quizCard) quizCard.classList.remove("superchat-glow");
  }

  // 支援 YouTube 表情符號渲染
  $("#quiz-message-text").innerHTML = renderEmojiHtml(quiz.message_text);

  const optionsDiv = $("#quiz-options");
  optionsDiv.innerHTML = "";
  quiz.options.forEach((opt, i) => {
    const btn = document.createElement("button");
    btn.className = "option-btn";
    btn.textContent = `${i + 1}. ${opt.display_name}`;
    btn.dataset.authorDbId = opt.author_db_id;
    btn.addEventListener("click", () => submitAnswer(opt.author_db_id));
    optionsDiv.appendChild(btn);
  });
}

let answering = false;

async function submitAnswer(answeredAuthorDbId) {
  if (answering) return;
  answering = true;

  const quiz = state.quizzes[state.currentIndex];
  $$("#quiz-options .option-btn").forEach((b) => (b.disabled = true));

  try {
    const result = await apiPost(`/api/channels/${state.selectedChannelDbId}/quiz/answer`, {
      message_db_id: quiz.message_db_id,
      correct_author_db_id: quiz.correct_author_db_id,
      option_author_db_ids: quiz.options.map((o) => o.author_db_id),
      difficulty: quiz.difficulty,
      answered_author_db_id: answeredAuthorDbId,
    });

    $$("#quiz-options .option-btn").forEach((btn) => {
      const aid = parseInt(btn.dataset.authorDbId, 10);
      if (aid === result.correct_author_db_id) {
        btn.classList.add("correct");
      } else if (aid === answeredAuthorDbId) {
        btn.classList.add("wrong");
      }
    });

    state.answers[state.currentIndex] = result.is_correct ? "correct" : "wrong";
    state.quizHistory.push({
      quiz,
      answeredAuthorDbId,
      isCorrect: result.is_correct,
    });

    renderProgressDots();

    setTimeout(() => {
      answering = false;
      if (state.currentIndex + 1 < state.quizzes.length) {
        state.currentIndex += 1;
        renderProgressDots();
        renderCurrentQuiz();
      } else {
        renderResult();
        goToStep("step-result");
      }
    }, 900);
  } catch (err) {
    answering = false;
    $$("#quiz-options .option-btn").forEach((b) => (b.disabled = false));
    showError("提交答案失敗：" + err.message);
  }
}

// 鍵盤快捷鍵：數字鍵 1-3 直接作答
document.addEventListener("keydown", (e) => {
  if (!$("#step-quiz").classList.contains("active")) return;
  if (answering) return;
  const num = parseInt(e.key, 10);
  if (num >= 1 && num <= 3) {
    const btn = $$("#quiz-options .option-btn")[num - 1];
    if (btn && !btn.disabled) btn.click();
  }
});

// ============ STEP 4: 結果 ============
function renderResult() {
  const total = state.answers.length;
  const correct = state.answers.filter((a) => a === "correct").length;
  const accuracy = total > 0 ? Math.round((correct / total) * 100) : 0;

  $("#result-accuracy").textContent = accuracy + "%";

  let maxStreak = 0, curStreak = 0;
  state.answers.forEach((a) => {
    if (a === "correct") {
      curStreak += 1;
      maxStreak = Math.max(maxStreak, curStreak);
    } else {
      curStreak = 0;
    }
  });

  $("#result-stats").innerHTML = `
    答對 ${correct} / ${total} 題<br>
    最高連對 ${maxStreak} 題
  `;

  // 渲染題目詳細回顧與時間戳連結清單
  const reviewList = $("#result-review-list");
  reviewList.innerHTML = "";

  state.quizHistory.forEach((item, index) => {
    const q = item.quiz;
    const correctOpt = q.options.find((o) => o.author_db_id === q.correct_author_db_id);
    const chosenOpt = q.options.find((o) => o.author_db_id === item.answeredAuthorDbId);

    const correctName = correctOpt ? correctOpt.display_name : "未知";
    const chosenName = chosenOpt ? chosenOpt.display_name : "未作答";

    let timestampHtml = "";
    if (q.video_id && q.time_in_seconds != null) {
      const timeStr = formatSeconds(q.time_in_seconds);
      const ytUrl = `https://www.youtube.com/watch?v=${q.video_id}&t=${Math.floor(q.time_in_seconds)}s`;
      timestampHtml = `
        <a class="timestamp-link" href="${ytUrl}" target="_blank" rel="noopener noreferrer" title="在新分頁開啟 YouTube 直播指定秒數">
          ▶ ${timeStr} 直播回放
        </a>
      `;
    }

    // 輔助函式：產生觀眾名稱 + 頻道連結 HTML
    function optionLinkHtml(opt, isCorrect, isChosen) {
      if (!opt) return "<span class=\"opt-unknown\">未知</span>";
      const url = opt.channel_url || (opt.author_id ? `https://www.youtube.com/channel/${opt.author_id}` : "");
      const name = escapeHtml(opt.display_name || "(未知觀眾)");
      const icon = url
        ? `<a class="opt-ch-link${isCorrect ? " opt-correct" : ""}${isChosen && !isCorrect ? " opt-chosen" : ""}"
              href="${url}" target="_blank" rel="noopener noreferrer"
              title="UC…: ${opt.author_id || ""}">
              ${name} ↗</a>`
        : `<span class="opt-ch-link${isCorrect ? " opt-correct" : ""}${isChosen && !isCorrect ? " opt-chosen" : ""}">${name}</span>`;
      return icon;
    }

    // 產生 3 個選項的頻道連結列
    const optionsRowHtml = q.options.map((opt) => {
      const isCorrect = opt.author_db_id === q.correct_author_db_id;
      const isChosen  = opt.author_db_id === item.answeredAuthorDbId;
      const marker = isCorrect ? "✓" : (isChosen ? "✕" : "　");
      return `<span class="opt-row">
        <span class="opt-marker ${isCorrect ? "correct" : (isChosen ? "wrong" : "")}">${marker}</span>
        ${optionLinkHtml(opt, isCorrect, isChosen)}
      </span>`;
    }).join("");

    const hasSC = Boolean(q.amount || q.message_type === "paid_message" || q.message_type === "superchat");
    const scBadgeHtml = hasSC
      ? `<span class="sc-review-chip">💰 SC ${escapeHtml(q.amount || "超級留言")}</span>`
      : "";

    const itemDiv = document.createElement("div");
    itemDiv.className = `review-item ${item.isCorrect ? "correct" : "wrong"}${hasSC ? " has-superchat" : ""}`;
    itemDiv.innerHTML = `
      <div class="review-header">
        <div class="review-header-left">
          <span class="review-index">第 ${index + 1} 題</span>
          ${scBadgeHtml}
        </div>
        <span class="review-badge ${item.isCorrect ? "correct" : "wrong"}">${item.isCorrect ? "✓ 答對" : "✕ 答錯"}</span>
      </div>
      <div class="review-message ${hasSC ? "sc-message-glow" : ""}">${renderEmojiHtml(q.message_text)}</div>
      <div class="review-options-row">${optionsRowHtml}</div>
      <div class="review-meta">
        <div class="review-stream-info">
          <span class="stream-title">${escapeHtml(q.stream_title || "")}</span>
          ${timestampHtml}
        </div>
      </div>
    `;
    reviewList.appendChild(itemDiv);
  });
}

$("#restart-btn").addEventListener("click", () => {
  goToStep("step-setup");
});

// ============ 除錯區塊 ============
async function refreshDebugChannelSelects() {
  try {
    const channels = await apiGet("/api/channels");
    [$("#debug-channel-select"), $("#debug-authors-channel-select")].forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = "";
      channels.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.channel_db_id;
        opt.textContent = c.channel_name || c.channel_id;
        sel.appendChild(opt);
      });
      if (prev) sel.value = prev;
    });
  } catch (err) {
    console.error(err);
  }
}

$("#debug-load-streams-btn").addEventListener("click", async () => {
  const channelDbId = $("#debug-channel-select").value;
  if (!channelDbId) return;
  try {
    const streams = await apiGet(`/api/channels/${channelDbId}/streams`);
    $("#debug-streams-output").textContent = JSON.stringify(streams, null, 2);
  } catch (err) {
    $("#debug-streams-output").textContent = "載入失敗：" + err.message;
  }
});

$("#debug-load-authors-btn").addEventListener("click", async () => {
  const channelDbId = $("#debug-authors-channel-select").value;
  if (!channelDbId) return;
  try {
    const authors = await apiGet(`/api/channels/${channelDbId}/authors`);
    const list = $("#debug-authors-list");
    list.innerHTML = "";
    authors.forEach((a) => {
      const div = document.createElement("div");
      div.className = "debug-author-item";
      div.innerHTML = `<span>${escapeHtml(a.display_name)}</span><span>${a.filtered_message_count} 則</span>`;
      div.addEventListener("click", async () => {
        try {
          const detail = await apiGet(`/api/authors/${a.author_db_id}`);
          $("#debug-author-detail").textContent = JSON.stringify(detail, null, 2);
        } catch (err) {
          $("#debug-author-detail").textContent = "載入失敗：" + err.message;
        }
      });
      list.appendChild(div);
    });
  } catch (err) {
    $("#debug-authors-list").textContent = "載入失敗：" + err.message;
  }
});

function escapeHtml(str) {
  if (str == null) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ============ 初始載入 ============
refreshChannels();
