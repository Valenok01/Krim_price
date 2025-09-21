const menuBtn = document.getElementById('menuBtn');
const dropdownMenu = document.getElementById('dropdownMenu');

menuBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    dropdownMenu.classList.toggle('active');
});

document.addEventListener('click', function(e) {
    if (!dropdownMenu.contains(e.target) && e.target !== menuBtn) {
        dropdownMenu.classList.remove('active');
    }
});

function scrollToContact() {
    document.getElementById('contact').scrollIntoView({ 
        behavior: 'smooth' 
    });
    dropdownMenu.classList.remove('active');
}

document.addEventListener('DOMContentLoaded', function() {
    const backgroundAnimation = document.querySelector('.background-animation');
    
    for (let i = 0; i < 10; i++) {
        const circle = document.createElement('div');
        circle.classList.add('circle');
        
        const size = Math.random() * 60 + 20;
        const top = Math.random() * 100;
        const left = Math.random() * 100;
        const delay = Math.random() * 5;
        const duration = Math.random() * 10 + 15;
        
        circle.style.width = `${size}px`;
        circle.style.height = `${size}px`;
        circle.style.top = `${top}%`;
        circle.style.left = `${left}%`;
        circle.style.animationDelay = `${delay}s`;
        circle.style.animationDuration = `${duration}s`;
        
        backgroundAnimation.appendChild(circle);
    }
});