const tokenInput = document.getElementById("token-input");
const saveBtn = document.getElementById("save-btn");
const statusEl = document.getElementById("status");

function loadOptions() {
  chrome.storage.local.get(["vid_token"], (result) => {
    if (result.vid_token) {
      tokenInput.value = result.vid_token;
    }
  });
}

function saveOptions() {
  const token = tokenInput.value.trim();
  chrome.storage.local.set({ vid_token: token }, () => {
    statusEl.textContent = "Token saved.";
    setTimeout(() => (statusEl.textContent = ""), 2000);
  });
}

saveBtn.addEventListener("click", saveOptions);
document.addEventListener("DOMContentLoaded", loadOptions);
loadOptions();
