// ============================================
// 3D-сцены погоды с реалистичными городами и природой
// ============================================

(function() {
    if (typeof THREE === 'undefined') {
        console.error('THREE is not loaded');
        return;
    }

    let scene, camera, renderer, composer;
    let particles = [];
    let cityGroup;
    let ambientLight, sunLight, backLight;
    let sunMesh;
    let clock = new THREE.Clock();
    let overlay = document.getElementById('sceneOverlay');
    let canvas = document.getElementById('scene-canvas');

    let currentCity = '';
    let currentType = '';
    let currentTemp = 0;
    let animationFrame;

    const rainCount = 2000;
    const snowCount = 1200;
    const cloudCount = 20;
    const lightningChance = 0.008;

    // Инициализация сцены
    function initScene() {
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x111122);
        scene.fog = new THREE.Fog(0x111122, 50, 200);

        camera = new THREE.PerspectiveCamera(65, window.innerWidth / window.innerHeight, 0.1, 1000);
        camera.position.set(0, 18, 70);
        camera.lookAt(0, 5, 0);

        renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
        renderer.setSize(window.innerWidth, window.innerHeight);
        renderer.shadowMap.enabled = true;
        renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        renderer.outputEncoding = THREE.sRGBEncoding;
        renderer.toneMapping = THREE.ACESFilmicToneMapping;
        renderer.toneMappingExposure = 1.5;

        // Пост-обработка Bloom
        if (typeof EffectComposer !== 'undefined' && typeof RenderPass !== 'undefined' && typeof UnrealBloomPass !== 'undefined') {
            const renderScene = new RenderPass(scene, camera);
            const bloomPass = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 1.5, 0.4, 0.85);
            bloomPass.threshold = 0.1;
            bloomPass.strength = 1.3;
            bloomPass.radius = 0.5;
            composer = new EffectComposer(renderer);
            composer.addPass(renderScene);
            composer.addPass(bloomPass);
        } else {
            composer = { render: () => renderer.render(scene, camera) };
        }

        // Освещение
        ambientLight = new THREE.AmbientLight(0x404060, 0.8);
        scene.add(ambientLight);

        sunLight = new THREE.DirectionalLight(0xffeedd, 1.8);
        sunLight.position.set(20, 25, 15);
        sunLight.castShadow = true;
        sunLight.shadow.mapSize.width = 2048;
        sunLight.shadow.mapSize.height = 2048;
        const d = 40;
        sunLight.shadow.camera.left = -d;
        sunLight.shadow.camera.right = d;
        sunLight.shadow.camera.top = d;
        sunLight.shadow.camera.bottom = -d;
        sunLight.shadow.camera.near = 1;
        sunLight.shadow.camera.far = 60;
        scene.add(sunLight);

        backLight = new THREE.PointLight(0x446688, 0.5);
        backLight.position.set(-15, 5, -30);
        scene.add(backLight);

        // Солнце
        const sunGeo = new THREE.SphereGeometry(1.5, 32, 32);
        const sunMat = new THREE.MeshStandardMaterial({ color: 0xffaa33, emissive: 0xff5500, emissiveIntensity: 3.0 });
        sunMesh = new THREE.Mesh(sunGeo, sunMat);
        sunMesh.position.set(20, 20, 15);
        scene.add(sunMesh);

        // Земля
        const groundGeo = new THREE.CircleGeometry(100, 64);
        const groundMat = new THREE.MeshStandardMaterial({ color: 0x2a5a2a, roughness: 0.8 });
        const ground = new THREE.Mesh(groundGeo, groundMat);
        ground.rotation.x = -Math.PI / 2;
        ground.position.y = -2;
        ground.receiveShadow = true;
        scene.add(ground);

        // Деревья (природа) – создаются один раз и остаются навсегда
        for (let i = 0; i < 60; i++) {
            const tree = new THREE.Group();
            const trunkGeo = new THREE.CylinderGeometry(0.3, 0.5, 3);
            const trunkMat = new THREE.MeshStandardMaterial({ color: 0x8B4513 });
            const trunk = new THREE.Mesh(trunkGeo, trunkMat);
            trunk.position.y = 1.5;
            trunk.castShadow = true;
            trunk.receiveShadow = true;
            tree.add(trunk);

            const foliageGeo = new THREE.ConeGeometry(1.0, 2.0, 6);
            const foliageMat = new THREE.MeshStandardMaterial({ color: 0x228833 });
            const foliage = new THREE.Mesh(foliageGeo, foliageMat);
            foliage.position.y = 3.5;
            foliage.castShadow = true;
            foliage.receiveShadow = true;
            tree.add(foliage);

            const angle = Math.random() * Math.PI * 2;
            const radius = 40 + Math.random() * 40;
            tree.position.set(Math.cos(angle) * radius, -1, Math.sin(angle) * radius);
            scene.add(tree);
        }

        // Трава (спрайты)
        const grassTex = (() => {
            const c = document.createElement('canvas');
            c.width = 8;
            c.height = 16;
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#3a8a3a';
            ctx.fillRect(0, 0, 8, 16);
            return new THREE.CanvasTexture(c);
        })();
        for (let i = 0; i < 1200; i++) {
            const grassMat = new THREE.SpriteMaterial({ map: grassTex, color: 0x88aa88, transparent: true });
            const grass = new THREE.Sprite(grassMat);
            const angle = Math.random() * Math.PI * 2;
            const radius = 30 + Math.random() * 50;
            grass.position.set(Math.cos(angle) * radius, -1.5 + Math.random() * 0.5, Math.sin(angle) * radius);
            grass.scale.set(0.5 + Math.random()*0.5, 1 + Math.random(), 1);
            scene.add(grass);
        }

        window.addEventListener('resize', onResize);
    }

    function onResize() {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
        if (composer && composer.setSize) composer.setSize(window.innerWidth, window.innerHeight);
    }

    // ========== УНИКАЛЬНЫЕ РЕАЛИСТИЧНЫЕ ГОРОДА (без наложений) ==========
    function createCitySilhouette(city) {
        const group = new THREE.Group();
        // Базовый цвет для города (изменяется под конкретный город)
        let baseColor, accentColor;
        switch(city) {
            case 'Дубай':
                baseColor = 0xaa8866; accentColor = 0xccaa88; break;
            case 'Москва':
                baseColor = 0x884422; accentColor = 0xaa6644; break;
            case 'Лондон':
                baseColor = 0x556677; accentColor = 0x778899; break;
            case 'Нью-Йорк':
                baseColor = 0x334455; accentColor = 0x556677; break;
            case 'Токио':
                baseColor = 0xcc9999; accentColor = 0xeeaaaa; break;
            case 'Сидней':
                baseColor = 0x88aaff; accentColor = 0xaaccff; break;
            case 'Рио-де-Жанейро':
                baseColor = 0x44aa88; accentColor = 0x66ccaa; break;
            case 'Кейптаун':
                baseColor = 0xaabbcc; accentColor = 0xccddee; break;
            default:
                baseColor = 0xaaaaaa; accentColor = 0xcccccc;
        }

        // Генерируем 25-30 зданий со случайными позициями, чтобы не накладывались
        const numBuildings = 25 + Math.floor(Math.random() * 10);
        for (let i = 0; i < numBuildings; i++) {
            // Случайное положение в пределах -20..20 по X и -15..15 по Z (чтобы не уходить далеко)
            const x = (Math.random() - 0.5) * 40;
            const z = (Math.random() - 0.5) * 30 - 10; // немного смещаем вглубь

            // Случайная высота и ширина
            const width = 0.8 + Math.random() * 1.5;
            const depth = 0.8 + Math.random() * 1.5;
            const height = 3 + Math.random() * 15;

            // Случайная форма: коробка, цилиндр, конус (иногда)
            let geom;
            if (Math.random() < 0.7) {
                geom = new THREE.BoxGeometry(width, height, depth);
            } else {
                // цилиндр (башня)
                geom = new THREE.CylinderGeometry(width*0.8, width, height, 6);
            }

            const color = Math.random() < 0.8 ? baseColor : accentColor;
            const mat = new THREE.MeshStandardMaterial({ color: color, roughness: 0.4, metalness: 0.1 });
            const building = new THREE.Mesh(geom, mat);
            building.position.set(x, height/2 - 2, z);
            building.castShadow = true;
            building.receiveShadow = true;
            group.add(building);

            // Иногда добавляем маленькую башенку сверху
            if (Math.random() < 0.3) {
                const spireGeo = new THREE.ConeGeometry(width*0.4, height*0.3, 4);
                const spireMat = new THREE.MeshStandardMaterial({ color: accentColor });
                const spire = new THREE.Mesh(spireGeo, spireMat);
                spire.position.set(x, height - 2, z);
                spire.castShadow = true;
                group.add(spire);
            }
        }

        // Уникальные достопримечательности (по одной-две)
        if (city === 'Дубай') {
            // Бурдж-Халифа (очень высокая)
            const towerGeo = new THREE.CylinderGeometry(1.5, 2.5, 35, 8);
            const towerMat = new THREE.MeshStandardMaterial({ color: 0xcccccc, emissive: 0x224466 });
            const tower = new THREE.Mesh(towerGeo, towerMat);
            tower.position.set(5, 15.5, -5);
            tower.castShadow = true;
            group.add(tower);
        } else if (city === 'Москва') {
            // Кремлёвская башня
            const baseGeo = new THREE.CylinderGeometry(1.2, 1.5, 15, 8);
            const baseMat = new THREE.MeshStandardMaterial({ color: 0x993333 });
            const base = new THREE.Mesh(baseGeo, baseMat);
            base.position.set(6, 5.5, -8);
            group.add(base);
            const spireGeo = new THREE.ConeGeometry(0.8, 8, 8);
            const spire = new THREE.Mesh(spireGeo, baseMat);
            spire.position.set(6, 13, -8);
            group.add(spire);
        } else if (city === 'Лондон') {
            // Биг-Бен
            const clockGeo = new THREE.BoxGeometry(1.5, 20, 1.5);
            const clockMat = new THREE.MeshStandardMaterial({ color: 0xaa8866 });
            const clock = new THREE.Mesh(clockGeo, clockMat);
            clock.position.set(-5, 8, -10);
            group.add(clock);
            const spireGeo = new THREE.ConeGeometry(0.8, 5, 8);
            spireGeo.position.set(-5, 18, -10);
            group.add(spireGeo);
        } else if (city === 'Нью-Йорк') {
            // Статуя Свободы
            const bodyGeo = new THREE.CylinderGeometry(1, 1.5, 12, 6);
            const bodyMat = new THREE.MeshStandardMaterial({ color: 0x2e8b57 });
            const body = new THREE.Mesh(bodyGeo, bodyMat);
            body.position.set(7, 4, -5);
            group.add(body);
            const headGeo = new THREE.SphereGeometry(1.2);
            const head = new THREE.Mesh(headGeo, bodyMat);
            head.position.set(7, 10, -5);
            group.add(head);
        } else if (city === 'Токио') {
            // Токийская башня
            const towerGeo = new THREE.CylinderGeometry(1.2, 1.8, 25, 8);
            const towerMat = new THREE.MeshStandardMaterial({ color: 0xcc6666 });
            const tower = new THREE.Mesh(towerGeo, towerMat);
            tower.position.set(4, 10.5, -8);
            group.add(tower);
        } else if (city === 'Рио-де-Жанейро') {
            // Христос-Искупитель
            const crossV = new THREE.BoxGeometry(0.8, 12, 0.8);
            const crossH = new THREE.BoxGeometry(6, 0.8, 0.8);
            const crossMat = new THREE.MeshStandardMaterial({ color: 0xaaaaaa });
            const v = new THREE.Mesh(crossV, crossMat);
            v.position.set(2, 4, -5);
            group.add(v);
            const h = new THREE.Mesh(crossH, crossMat);
            h.position.set(2, 8, -5);
            group.add(h);
        } else if (city === 'Кейптаун') {
            // Столовая гора (плоская вершина)
            const plateauGeo = new THREE.BoxGeometry(20, 8, 12);
            const plateauMat = new THREE.MeshStandardMaterial({ color: 0x7a7a7a });
            const plateau = new THREE.Mesh(plateauGeo, plateauMat);
            plateau.position.set(0, 2, -20);
            group.add(plateau);
        } else if (city === 'Сидней') {
            // Оперный театр
            for (let i = 0; i < 4; i++) {
                const sailGeo = new THREE.ConeGeometry(1.5, 5, 4);
                const sailMat = new THREE.MeshStandardMaterial({ color: 0xffffff });
                const sail = new THREE.Mesh(sailGeo, sailMat);
                sail.position.set(i*2 - 3, 1.5, -8);
                sail.rotation.y = 0.3 * i;
                group.add(sail);
            }
        }

        return group;
    }

    // ========== ОБЛАКА ==========
    function createClouds(count) {
        const group = new THREE.Group();
        for (let i = 0; i < count; i++) {
            const cloud = new THREE.Group();
            const parts = 3 + Math.floor(Math.random() * 4);
            for (let j = 0; j < parts; j++) {
                const sphereGeom = new THREE.SphereGeometry(1 + Math.random()*0.8, 7);
                const sphereMat = new THREE.MeshStandardMaterial({ color: 0xeeeeee, transparent: true, opacity: 0.4 });
                const sphere = new THREE.Mesh(sphereGeom, sphereMat);
                sphere.position.set((j-1)*2.0, Math.sin(j)*0.5, 0);
                sphere.castShadow = false;
                sphere.receiveShadow = false;
                cloud.add(sphere);
            }
            cloud.position.set((Math.random()-0.5)*80, 20 + Math.random()*20, (Math.random()-0.5)*60);
            cloud.userData = { speed: 0.02 + Math.random()*0.03, dir: Math.random()>0.5 ? 1 : -1 };
            group.add(cloud);
        }
        return group;
    }

    // ========== ЧАСТИЦЫ (дождь/снег) ==========
    function createRainParticles(count) {
        const geom = new THREE.BufferGeometry();
        const pos = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) {
            pos[i*3] = (Math.random() - 0.5) * 120;
            pos[i*3+1] = Math.random() * 60;
            pos[i*3+2] = (Math.random() - 0.5) * 100;
        }
        geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        const mat = new THREE.PointsMaterial({ color: 0xaaccff, size: 0.2, transparent: true, opacity: 0.7 });
        const rain = new THREE.Points(geom, mat);
        rain.userData = { speed: 0.4, type: 'rain' };
        return rain;
    }

    function createSnowParticles(count) {
        const geom = new THREE.BufferGeometry();
        const pos = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) {
            pos[i*3] = (Math.random() - 0.5) * 120;
            pos[i*3+1] = Math.random() * 60;
            pos[i*3+2] = (Math.random() - 0.5) * 100;
        }
        geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        const mat = new THREE.PointsMaterial({ color: 0xffffff, size: 0.25, transparent: true });
        const snow = new THREE.Points(geom, mat);
        snow.userData = { speed: 0.15, type: 'snow' };
        return snow;
    }

    // ========== НАДПИСЬ ГОРОДА ==========
    function createCityLabel(city) {
        const canvas = document.createElement('canvas');
        canvas.width = 512;
        canvas.height = 128;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = 'rgba(0,0,0,0.5)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.font = 'Bold 70px "Inter", sans-serif';
        ctx.fillStyle = '#ff6600';
        ctx.shadowColor = '#ff6600';
        ctx.shadowBlur = 15;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(city, canvas.width/2, canvas.height/2);
        const tex = new THREE.CanvasTexture(canvas);
        const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
        sprite.scale.set(12, 3, 1);
        sprite.position.set(0, 25, 0);
        sprite.userData = { alpha: 1.0 };
        return sprite;
    }

    // ========== ОЧИСТКА СЦЕНЫ (сохраняем природу) ==========
    function clearScene() {
        // Удаляем только город, облака и частицы
        const toRemove = [];
        scene.children.forEach(child => {
            if (child === ambientLight || child === sunLight || child === backLight || child === sunMesh) return;
            if (child.isMesh && child.material && child.material.color.getHex() === 0x2a5a2a) return; // земля
            if (child.isGroup && child.children.length && child.children[0].material && child.children[0].material.color.getHex() === 0x228833) return; // деревья
            if (child.isSprite && child.material.map && child.material.map.image && child.material.map.image.width === 8) return; // трава
            toRemove.push(child);
        });
        toRemove.forEach(child => scene.remove(child));
        particles = [];
    }

    // ========== АНИМАЦИЯ ЧАСТИЦ ==========
    function animateParticles(delta) {
        particles.forEach(p => {
            const pos = p.geometry.attributes.position.array;
            if (p.userData.type === 'rain') {
                for (let i = 1; i < pos.length; i+=3) {
                    pos[i] -= p.userData.speed * delta * 40;
                    pos[i-1] += Math.sin(Date.now()*0.002 + pos[i]) * 0.1;
                    if (pos[i] < -5) {
                        pos[i] = 55;
                        pos[i-1] = (Math.random()-0.5)*120;
                        pos[i+1] = (Math.random()-0.5)*100;
                    }
                }
            } else if (p.userData.type === 'snow') {
                for (let i = 1; i < pos.length; i+=3) {
                    pos[i] -= p.userData.speed * delta * 30;
                    pos[i-1] += Math.sin(Date.now()*0.001 + pos[i]) * 0.05;
                    if (pos[i] < -5) {
                        pos[i] = 55;
                        pos[i-1] = (Math.random()-0.5)*120;
                        pos[i+1] = (Math.random()-0.5)*100;
                    }
                }
            }
            p.geometry.attributes.position.needsUpdate = true;
        });
    }

    // ========== МОЛНИЯ ==========
    function createLightning() {
        const points = [];
        let x = (Math.random()-0.5)*40;
        let y = 30;
        let z = (Math.random()-0.5)*30;
        for (let i=0; i<14; i++) {
            points.push(new THREE.Vector3(x, y, z));
            x += (Math.random()-0.5)*6;
            y -= 2.5 + Math.random()*5;
            z += (Math.random()-0.5)*4;
        }
        const geom = new THREE.BufferGeometry().setFromPoints(points);
        const line = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: 0xffdd88 }));
        scene.add(line);
        setTimeout(() => scene.remove(line), 120);
        const flashLight = new THREE.PointLight(0xffaa88, 4, 40);
        flashLight.position.set(points[0].x, points[0].y, points[0].z);
        scene.add(flashLight);
        setTimeout(() => scene.remove(flashLight), 100);
    }

    // ========== ГЛАВНАЯ АНИМАЦИЯ ==========
    function animate() {
        if (!overlay.classList.contains('active')) return;
        const delta = clock.getDelta();

        // Движение солнца
        sunMesh.rotation.y += 0.003;
        sunMesh.position.x = 20 + Math.sin(Date.now()*0.001)*4;
        sunLight.position.copy(sunMesh.position);

        // Движение облаков
        scene.children.forEach(child => {
            if (child.isGroup && child.userData.speed) {
                child.position.x += child.userData.dir * child.userData.speed * delta * 30;
                if (child.position.x > 60) child.position.x = -60;
                if (child.position.x < -60) child.position.x = 60;
            }
        });

        // Исчезновение надписи
        scene.children.forEach(child => {
            if (child.isSprite && child.userData.alpha !== undefined) {
                child.material.opacity = child.userData.alpha;
                child.userData.alpha -= 0.002;
                if (child.userData.alpha <= 0) scene.remove(child);
            }
        });

        animateParticles(delta);
        if (currentType === 'stormy' && Math.random() < lightningChance) createLightning();

        composer.render();
        animationFrame = requestAnimationFrame(animate);
    }

    // ========== ГЛОБАЛЬНАЯ ФУНКЦИЯ ВЫЗОВА ==========
    window.showWeatherScene = function(city, type, temp) {
        clearScene();
        currentCity = city;
        currentType = type;
        currentTemp = temp;

        // Настройка фона
        if (type === 'sunny') {
            scene.background.setHex(0x87CEEB);
            scene.fog.color.setHex(0x87CEEB);
            ambientLight.intensity = 0.9;
            sunLight.intensity = 2.5;
            sunMesh.material.emissiveIntensity = 4.0;
        } else if (type === 'cloudy') {
            scene.background.setHex(0x778899);
            scene.fog.color.setHex(0x778899);
            ambientLight.intensity = 0.7;
            sunLight.intensity = 1.2;
            sunMesh.material.emissiveIntensity = 1.5;
        } else if (type === 'rainy') {
            scene.background.setHex(0x2F4F4F);
            scene.fog.color.setHex(0x2F4F4F);
            ambientLight.intensity = 0.5;
            sunLight.intensity = 0.5;
            sunMesh.material.emissiveIntensity = 0.3;
        } else if (type === 'snowy') {
            scene.background.setHex(0xA9A9A9);
            scene.fog.color.setHex(0xA9A9A9);
            ambientLight.intensity = 0.8;
            sunLight.intensity = 1.0;
            sunMesh.material.emissiveIntensity = 1.0;
        } else if (type === 'stormy') {
            scene.background.setHex(0x1A1A2E);
            scene.fog.color.setHex(0x1A1A2E);
            ambientLight.intensity = 0.3;
            sunLight.intensity = 0.3;
            sunMesh.material.emissiveIntensity = 0.0;
        }

        if (type === 'rainy' || type === 'stormy') {
            const rain = createRainParticles(rainCount);
            scene.add(rain);
            particles.push(rain);
        }
        if (type === 'snowy') {
            const snow = createSnowParticles(snowCount);
            scene.add(snow);
            particles.push(snow);
        }

        if (type !== 'sunny') {
            const clouds = createClouds(cloudCount);
            scene.add(clouds);
        }

        cityGroup = createCitySilhouette(city);
        scene.add(cityGroup);

        const label = createCityLabel(city);
        scene.add(label);

        overlay.classList.add('active');
        if (animationFrame) cancelAnimationFrame(animationFrame);
        animate();
    };

    window.closeScene = function() {
        overlay.classList.remove('active');
        if (animationFrame) {
            cancelAnimationFrame(animationFrame);
            animationFrame = null;
        }
    };

    initScene();
})();