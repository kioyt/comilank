// Переключение вкладок
function openTab(evt, tabName) {
    let i, tabcontent, tabbuttons;

    tabcontent = document.getElementsByClassName("tab-content");
    for (i = 0; i < tabcontent.length; i++) {
        tabcontent[i].classList.remove("active");
    }

    tabbuttons = document.getElementsByClassName("tab-button");
    for (i = 0; i < tabbuttons.length; i++) {
        tabbuttons[i].classList.remove("active");
    }

    document.getElementById(tabName).classList.add("active");
    evt.currentTarget.classList.add("active");
}

// Вращение барабана (добавляем класс анимации на сетку)
function spinGames() {
    const grid = document.getElementById("gamesGrid");
    grid.classList.add("spin-animation");
    setTimeout(() => {
        grid.classList.remove("spin-animation");
    }, 1000); // убираем класс через 1 секунду
}

// Обработчик клика на карточки
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('game-card')) {
        const game = e.target.getAttribute('data-game') || e.target.innerText;
        alert(`Ты выбрал игру: ${game}`);
    }
});