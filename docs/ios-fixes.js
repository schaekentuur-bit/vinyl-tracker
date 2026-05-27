(function(){
  document.addEventListener('DOMContentLoaded',function(){
    var _t=false;
    var btn=document.querySelector('.hamburger');
    if(btn){
      btn.removeAttribute('onclick');
      btn.addEventListener('touchend',function(e){
        _t=true;e.preventDefault();e.stopPropagation();
        if(typeof toggleNav==='function')toggleNav();
        setTimeout(function(){_t=false;},300);
      },{passive:false});
      btn.addEventListener('click',function(e){
        if(_t){_t=false;return;}
        e.stopPropagation();
        if(typeof toggleNav==='function')toggleNav();
      });
    }
    var navEl=document.querySelector('nav');
    if(navEl){
      navEl.addEventListener('touchend',function(e){
        var item=e.target.closest('.nav-item[data-page]');
        if(item){
          e.preventDefault();e.stopPropagation();
          navEl.classList.remove('open');
          var ov=document.getElementById('nav-overlay');
          if(ov)ov.classList.remove('open');
          if(typeof showPage==='function')showPage(item.getAttribute('data-page'));
        }
      },{passive:false});
    }
  });
})();
