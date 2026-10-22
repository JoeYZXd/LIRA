(() => {
  'use strict';
  if(window.parent===window)return;
  const report=()=>window.parent.postMessage({type:'lira:teaching-size',height:document.querySelector('main').getBoundingClientRect().height+24},location.origin);
  new ResizeObserver(report).observe(document.querySelector('main'));
  report();
})();
