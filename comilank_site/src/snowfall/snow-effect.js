// ============================================
// Дополнительный слой снега с astro-snowfall
// ============================================

// Эта библиотека уже установлена через npm
// Используем её для создания метели

import Snowfall from 'astro-snowfall';

// Функция для запуска снегопада на overlay (если нужно)
// Но поскольку мы уже используем Three.js, этот файл можно оставить пустым.
// Однако, если хочешь добавить ещё один слой, можно создать элемент canvas поверх и запустить Snowfall.

// Пример:
// const canvas = document.createElement('canvas');
// canvas.style.position = 'fixed';
// canvas.style.top = 0;
// canvas.style.left = 0;
// canvas.style.width = '100%';
// canvas.style.height = '100%';
// canvas.style.pointerEvents = 'none';
// document.body.appendChild(canvas);
// 
// new Snowfall({
//     canvas: canvas,
//     snowflakeCount: 500,
//     color: '#ffffff',
//     speed: [1, 3],
//     wind: [-1, 2],
//     enable3DRotation: true
// });