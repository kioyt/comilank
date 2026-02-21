// ============================================
// Кинематографичные анимированные иконки погоды
// ============================================

(function() {
    const canvasCache = new Map();
    
    function drawIcon(ctx, type, size, time) {
        const w = size;
        const h = size;
        ctx.clearRect(0, 0, w, h);
        
        const centerX = w / 2;
        const centerY = h / 2;
        const radius = w * 0.25;
        
        // Общие тени для объёма
        ctx.shadowColor = '#ff6600';
        ctx.shadowBlur = 15;
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 0;
        
        if (type === 'sunny') {
            // Солнце с пульсирующими лучами
            const pulse = 1 + Math.sin(time * 0.01) * 0.1;
            ctx.fillStyle = '#FFD700';
            ctx.beginPath();
            ctx.arc(centerX, centerY, radius * pulse, 0, 2 * Math.PI);
            ctx.fill();
            
            ctx.strokeStyle = '#FFA500';
            ctx.lineWidth = 3;
            for (let i = 0; i < 12; i++) {
                const angle = i * Math.PI / 6 + time * 0.002;
                const dx = Math.cos(angle) * radius * 2;
                const dy = Math.sin(angle) * radius * 2;
                ctx.beginPath();
                ctx.moveTo(centerX, centerY);
                ctx.lineTo(centerX + dx, centerY + dy);
                ctx.stroke();
            }
        } else if (type === 'cloudy') {
            // Облако с мягким свечением
            ctx.fillStyle = '#FFFFFF';
            ctx.shadowColor = '#AAAAAA';
            ctx.beginPath();
            ctx.arc(centerX - 10, centerY - 5, radius * 0.8, 0, 2 * Math.PI);
            ctx.arc(centerX + 10, centerY - 8, radius * 0.9, 0, 2 * Math.PI);
            ctx.arc(centerX, centerY, radius, 0, 2 * Math.PI);
            ctx.fill();
        } else if (type === 'rainy') {
            // Облако с каплями
            ctx.fillStyle = '#CCCCCC';
            ctx.beginPath();
            ctx.arc(centerX - 8, centerY - 8, radius * 0.7, 0, 2 * Math.PI);
            ctx.arc(centerX + 8, centerY - 10, radius * 0.8, 0, 2 * Math.PI);
            ctx.arc(centerX, centerY - 5, radius * 0.8, 0, 2 * Math.PI);
            ctx.fill();
            
            ctx.fillStyle = '#00BFFF';
            ctx.shadowColor = '#00BFFF';
            for (let i = 0; i < 7; i++) {
                const x = centerX - 20 + i * 8 + Math.sin(time * 0.01 + i) * 2;
                const y = centerY + 8 + Math.sin(time * 0.02 + i) * 3;
                ctx.beginPath();
                ctx.ellipse(x, y, 2, 5, 0, 0, 2 * Math.PI);
                ctx.fill();
            }
        } else if (type === 'snowy') {
            // Облако со снежинками
            ctx.fillStyle = '#EEEEEE';
            ctx.beginPath();
            ctx.arc(centerX - 8, centerY - 8, radius * 0.7, 0, 2 * Math.PI);
            ctx.arc(centerX + 8, centerY - 10, radius * 0.8, 0, 2 * Math.PI);
            ctx.arc(centerX, centerY - 5, radius * 0.8, 0, 2 * Math.PI);
            ctx.fill();
            
            ctx.strokeStyle = '#FFFFFF';
            ctx.fillStyle = '#FFFFFF';
            ctx.lineWidth = 1.5;
            for (let i = 0; i < 6; i++) {
                const x = centerX - 15 + i * 8 + Math.sin(time * 0.005 + i) * 2;
                const y = centerY + 10 + Math.cos(time * 0.003 + i) * 3;
                ctx.save();
                ctx.translate(x, y);
                ctx.rotate(time * 0.001 + i);
                for (let j = 0; j < 6; j++) {
                    const angle = j * Math.PI / 3;
                    ctx.beginPath();
                    ctx.moveTo(0, 0);
                    ctx.lineTo(Math.cos(angle) * 6, Math.sin(angle) * 6);
                    ctx.stroke();
                }
                ctx.restore();
            }
        } else if (type === 'stormy') {
            // Облако с молнией
            ctx.fillStyle = '#555555';
            ctx.beginPath();
            ctx.arc(centerX - 8, centerY - 8, radius * 0.7, 0, 2 * Math.PI);
            ctx.arc(centerX + 8, centerY - 10, radius * 0.8, 0, 2 * Math.PI);
            ctx.arc(centerX, centerY - 5, radius * 0.8, 0, 2 * Math.PI);
            ctx.fill();
            
            ctx.strokeStyle = '#FFFF00';
            ctx.shadowColor = '#FFFF00';
            ctx.lineWidth = 4;
            ctx.beginPath();
            ctx.moveTo(centerX - 10, centerY + 5);
            ctx.lineTo(centerX - 5, centerY - 5);
            ctx.lineTo(centerX + 5, centerY);
            ctx.lineTo(centerX, centerY + 10);
            ctx.stroke();
        }
    }
    
    function updateAllIcons() {
        const cards = document.querySelectorAll('.weather-card');
        const time = Date.now();
        cards.forEach(card => {
            const descElem = card.querySelector('.weather-desc');
            if (!descElem) return;
            const desc = descElem.textContent;
            let type = 'sunny';
            if (desc.includes('Дождь')) type = 'rainy';
            else if (desc.includes('Снег')) type = 'snowy';
            else if (desc.includes('Гроза')) type = 'stormy';
            else if (desc.includes('Облачно')) type = 'cloudy';
            else if (desc.includes('Ясно')) type = 'sunny';
            
            let iconCanvas = card.querySelector('.weather-icon-canvas');
            if (!iconCanvas) {
                iconCanvas = document.createElement('canvas');
                iconCanvas.className = 'weather-icon-canvas';
                iconCanvas.width = 64;
                iconCanvas.height = 64;
                iconCanvas.style.width = '48px';
                iconCanvas.style.height = '48px';
                const iconElem = card.querySelector('.weather-icon');
                if (iconElem) {
                    iconElem.innerHTML = '';
                    iconElem.appendChild(iconCanvas);
                }
            }
            const ctx = iconCanvas.getContext('2d');
            drawIcon(ctx, type, 64, time);
        });
        requestAnimationFrame(updateAllIcons);
    }
    
    window.addEventListener('load', () => {
        requestAnimationFrame(updateAllIcons);
    });
})();