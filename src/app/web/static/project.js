const projectThemeButton = document.getElementById('project-theme');
function syncProjectTheme() {
  const label = window.youjuTheme.current() === 'dark' ? '切换到浅色' : '切换到深色';
  projectThemeButton.setAttribute('aria-label', label);
  projectThemeButton.title = label;
}
projectThemeButton.onclick = () => window.youjuTheme.toggle();
document.addEventListener('youju:theme', syncProjectTheme);
syncProjectTheme();
