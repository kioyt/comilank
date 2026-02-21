// ==========================================================
// MAXIMUM CINEMATIC WEATHER SCENES WITH UNIQUE CITIES
// ==========================================================

(function() {
    if (typeof THREE === 'undefined') {
        console.error('THREE is not loaded');
        return;
    }

    // --- GLOBALS ---
    let scene, camera, renderer, composer;
    let particles = [];                 // weather particles (rain, snow)
    let cityGroup, natureGroup;
    let ambientLight, sunLight, backLight, windowLights = [];
    let sunMesh;
    let clock = new THREE.Clock();
    let overlay = document.getElementById('sceneOverlay');
    let canvas = document.getElementById('scene-canvas');
    let cloudNoiseTexture, rainTexture, snowTexture, leafTexture;

    let currentCity = '';
    let currentType = '';
    let currentTemp = 0;
    let animationFrame;

    // Particle counts (increased for realism)
    const RAIN_COUNT = 5000;
    const SNOW_COUNT = 3000;
    const CLOUD_COUNT = 30;
    const LIGHTNING_CHANCE = 0.012;
    const TREE_COUNT = 80;
    const GRASS_COUNT = 1500;
    const FLOWER_COUNT = 200;

    // --- UTILITY: generate procedural textures ---
    function createNoiseTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 128;
        canvas.height = 128;
        const ctx = canvas.getContext('2d');
        const imageData = ctx.createImageData(128, 128);
        for (let i = 0; i < 128 * 128; i++) {
            const val = Math.floor(Math.random() * 255);
            imageData.data[i*4] = val;
            imageData.data[i*4+1] = val;
            imageData.data[i*4+2] = val;
            imageData.data[i*4+3] = 255;
        }
        ctx.putImageData(imageData, 0, 0);
        return new THREE.CanvasTexture(canvas);
    }

    function createRainTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 8;
        canvas.height = 20;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#aaccff';
        ctx.shadowColor = '#aaccff';
        ctx.shadowBlur = 5;
        ctx.fillRect(2, 0, 4, 20);
        return new THREE.CanvasTexture(canvas);
    }

    function createSnowTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 16;
        canvas.height = 16;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.shadowColor = '#ffffff';
        ctx.shadowBlur = 8;
        ctx.beginPath();
        ctx.arc(8, 8, 5, 0, 2*Math.PI);
        ctx.fill();
        // add arms
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        for (let i=0; i<6; i++) {
            const angle = i * Math.PI/3;
            ctx.beginPath();
            ctx.moveTo(8,8);
            ctx.lineTo(8+Math.cos(angle)*10, 8+Math.sin(angle)*10);
            ctx.stroke();
        }
        return new THREE.CanvasTexture(canvas);
    }

    function createLeafTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 16;
        canvas.height = 16;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#3a9a3a';
        ctx.beginPath();
        ctx.ellipse(8,8,5,3,0,0,2*Math.PI);
        ctx.fill();
        ctx.fillStyle = '#2a7a2a';
        ctx.beginPath();
        ctx.ellipse(5,5,3,5,0.2,0,2*Math.PI);
        ctx.fill();
        return new THREE.CanvasTexture(canvas);
    }

    // --- INIT THREE.JS WITH BLOOM AND ADVANCED SETTINGS ---
    function initScene() {
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x0a0a1a);
        scene.fog = new THREE.Fog(0x0a0a1a, 50, 300);

        camera = new THREE.PerspectiveCamera(65, window.innerWidth / window.innerHeight, 0.1, 1000);
        camera.position.set(0, 12, 45);
        camera.lookAt(0, 5, 0);

        renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
        renderer.setSize(window.innerWidth, window.innerHeight);
        renderer.shadowMap.enabled = true;
        renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        renderer.outputEncoding = THREE.sRGBEncoding;
        renderer.toneMapping = THREE.ACESFilmicToneMapping;
        renderer.toneMappingExposure = 1.6;

        // Post-processing (Bloom)
        if (typeof EffectComposer !== 'undefined' && typeof RenderPass !== 'undefined' && typeof UnrealBloomPass !== 'undefined') {
            const renderScene = new RenderPass(scene, camera);
            const bloomPass = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 1.8, 0.4, 0.9);
            bloomPass.threshold = 0.1;
            bloomPass.strength = 1.5;
            bloomPass.radius = 0.6;
            composer = new EffectComposer(renderer);
            composer.addPass(renderScene);
            composer.addPass(bloomPass);
        } else {
            composer = { render: () => renderer.render(scene, camera) };
        }

        // --- LIGHTING ---
        ambientLight = new THREE.AmbientLight(0x404060, 0.9);
        scene.add(ambientLight);

        sunLight = new THREE.DirectionalLight(0xffeedd, 2.0);
        sunLight.position.set(25, 30, 20);
        sunLight.castShadow = true;
        sunLight.shadow.mapSize.width = 4096;
        sunLight.shadow.mapSize.height = 4096;
        const d = 60;
        sunLight.shadow.camera.left = -d;
        sunLight.shadow.camera.right = d;
        sunLight.shadow.camera.top = d;
        sunLight.shadow.camera.bottom = -d;
        sunLight.shadow.camera.near = 1;
        sunLight.shadow.camera.far = 80;
        scene.add(sunLight);

        backLight = new THREE.PointLight(0x446688, 0.6);
        backLight.position.set(-20, 8, -40);
        scene.add(backLight);

        // Sun sphere (visual)
        const sunGeo = new THREE.SphereGeometry(2.0, 64, 64);
        const sunMat = new THREE.MeshStandardMaterial({
            color: 0xffaa33,
            emissive: 0xff5500,
            emissiveIntensity: 3.5
        });
        sunMesh = new THREE.Mesh(sunGeo, sunMat);
        sunMesh.position.set(25, 25, 20);
        scene.add(sunMesh);

        // --- GROUND with displacement (hill simulation) ---
        const groundGeo = new THREE.CircleGeometry(120, 128);
        const groundMat = new THREE.MeshStandardMaterial({ color: 0x2a5a2a, roughness: 0.7, emissive: 0x000000 });
        const ground = new THREE.Mesh(groundGeo, groundMat);
        ground.rotation.x = -Math.PI / 2;
        ground.position.y = -2;
        ground.receiveShadow = true;
        scene.add(ground);

        // --- NATURE: trees, grass, flowers ---
        natureGroup = new THREE.Group();
        const leafTex = createLeafTexture();
        // Trees
        for (let i = 0; i < TREE_COUNT; i++) {
            const tree = new THREE.Group();
            const trunkGeo = new THREE.CylinderGeometry(0.4, 0.7, 3);
            const trunkMat = new THREE.MeshStandardMaterial({ color: 0x8B4513 });
            const trunk = new THREE.Mesh(trunkGeo, trunkMat);
            trunk.position.y = 1.5;
            trunk.castShadow = true;
            trunk.receiveShadow = true;
            tree.add(trunk);

            // Leaves (multiple spheres for realism)
            const leavesMat = new THREE.MeshStandardMaterial({ color: 0x228833, emissive: 0x112211 });
            for (let j = 0; j < 5; j++) {
                const leafGeo = new THREE.SphereGeometry(0.6 + Math.random()*0.4, 5);
                const leaf = new THREE.Mesh(leafGeo, leavesMat);
                leaf.position.set((Math.random()-0.5)*1.2, 2.5 + Math.random()*1.5, (Math.random()-0.5)*1.2);
                leaf.castShadow = true;
                leaf.receiveShadow = true;
                tree.add(leaf);
            }
            // Place tree in a ring
            const angle = Math.random() * Math.PI * 2;
            const radius = 30 + Math.random() * 40;
            tree.position.set(Math.cos(angle)*radius, -1.5, Math.sin(angle)*radius);
            natureGroup.add(tree);
        }

        // Grass (sprites)
        const grassTex = (() => {
            const c = document.createElement('canvas');
            c.width = 8; c.height = 16;
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#3a8a3a';
            ctx.fillRect(0,0,8,16);
            return new THREE.CanvasTexture(c);
        })();
        for (let i = 0; i < GRASS_COUNT; i++) {
            const grassMat = new THREE.SpriteMaterial({ map: grassTex, color: 0xaaddaa, transparent: true });
            const grass = new THREE.Sprite(grassMat);
            const angle = Math.random() * Math.PI * 2;
            const radius = 20 + Math.random() * 50;
            grass.position.set(Math.cos(angle)*radius, -1.8 + Math.random()*0.5, Math.sin(angle)*radius);
            grass.scale.set(0.3 + Math.random()*0.4, 0.8 + Math.random()*0.8, 1);
            natureGroup.add(grass);
        }

        // Flowers
        for (let i = 0; i < FLOWER_COUNT; i++) {
            const flowerMat = new THREE.SpriteMaterial({ color: Math.random()*0xffffff, map: leafTex, transparent: true });
            const flower = new THREE.Sprite(flowerMat);
            const angle = Math.random() * Math.PI * 2;
            const radius = 25 + Math.random() * 45;
            flower.position.set(Math.cos(angle)*radius, -1.7, Math.sin(angle)*radius);
            flower.scale.set(0.2, 0.2, 1);
            natureGroup.add(flower);
        }
        scene.add(natureGroup);

        // Procedural textures for clouds
        cloudNoiseTexture = createNoiseTexture();
        rainTexture = createRainTexture();
        snowTexture = createSnowTexture();

        window.addEventListener('resize', onResize);
    }

    function onResize() {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
        if (composer && composer.setSize) composer.setSize(window.innerWidth, window.innerHeight);
    }

    // ========== UNIQUE CITY GENERATORS ==========
    function createCity(city) {
        const group = new THREE.Group();
        const colors = {
            'Дубай': 0xaa8866,
            'Москва': 0x884422,
            'Лондон': 0x556677,
            'Нью-Йорк': 0x334455,
            'Токио': 0xcc9999,
            'Сидней': 0x88aaff,
            'Рио-де-Жанейро': 0x44aa88,
            'Кейптаун': 0xaabbcc
        };
        const baseColor = colors[city] || 0xaaaaaa;

        // Base buildings (random height/width)
        for (let i = 0; i < 40; i++) {
            const w = 0.8 + Math.random() * 2.0;
            const h = 5 + Math.random() * 25;
            const d = 0.8 + Math.random() * 2.0;
            const geom = new THREE.BoxGeometry(w, h, d);
            const mat = new THREE.MeshStandardMaterial({ color: baseColor, roughness: 0.3, emissive: 0x000000 });
            const building = new THREE.Mesh(geom, mat);
            building.position.set((i - 20) * 3.2, h/2 - 2, -20);
            building.castShadow = true;
            building.receiveShadow = true;
            group.add(building);
            // Windows
            for (let f = 0; f < 8; f++) {
                if (Math.random() > 0.5) continue;
                const winGeo = new THREE.BoxGeometry(0.1, 0.1, 0.1);
                const winMat = new THREE.MeshStandardMaterial({ color: 0xffaa33, emissive: 0xff5500 });
                const win = new THREE.Mesh(winGeo, winMat);
                win.position.set((i - 20) * 3.2, h * 0.2 + f * (h/8), -19.5);
                group.add(win);
            }
        }

        // Unique landmarks
        if (city === 'Дубай') {
            // Burj Khalifa
            const towerGeo = new THREE.CylinderGeometry(1.5, 2.5, 40, 8);
            const towerMat = new THREE.MeshStandardMaterial({ color: 0xcccccc, emissive: 0x224466 });
            const tower = new THREE.Mesh(towerGeo, towerMat);
            tower.position.set(8, 18, -20);
            group.add(tower);
            // Palm island (simplified)
            for (let i=0; i<7; i++) {
                const leafGeo = new THREE.ConeGeometry(1.5, 8, 6);
                const leafMat = new THREE.MeshStandardMaterial({ color: 0x88aa33 });
                const leaf = new THREE.Mesh(leafGeo, leafMat);
                leaf.position.set(8 + Math.cos(i*1.2)*6, 3, -20 + Math.sin(i*1.2)*6);
                group.add(leaf);
            }
        } else if (city === 'Москва') {
            // Kremlin spires
            for (let i=0; i<5; i++) {
                const spireGeo = new THREE.ConeGeometry(0.8, 12, 6);
                const spireMat = new THREE.MeshStandardMaterial({ color: 0x993333 });
                const spire = new THREE.Mesh(spireGeo, spireMat);
                spire.position.set(i*5 - 8, 4, -20);
                group.add(spire);
            }
        } else if (city === 'Лондон') {
            // Big Ben
            const clockGeo = new THREE.BoxGeometry(2, 20, 2);
            const clockMat = new THREE.MeshStandardMaterial({ color: 0xaa8866 });
            const clock = new THREE.Mesh(clockGeo, clockMat);
            clock.position.set(-10, 8, -20);
            group.add(clock);
            // London Eye (circle)
            const wheel = new THREE.Group();
            for (let i=0; i<12; i++) {
                const spoke = new THREE.BoxGeometry(0.2, 10, 0.2);
                const s = new THREE.Mesh(spoke, new THREE.MeshStandardMaterial({ color: 0xcccccc }));
                s.rotation.z = i * Math.PI/6;
                wheel.add(s);
            }
            wheel.position.set(15, 12, -20);
            group.add(wheel);
        } else if (city === 'Нью-Йорк') {
            // Statue of Liberty (abstract)
            const bodyGeo = new THREE.CylinderGeometry(1, 2, 12, 6);
            const bodyMat = new THREE.MeshStandardMaterial({ color: 0x2e8b57 });
            const body = new THREE.Mesh(bodyGeo, bodyMat);
            body.position.set(12, 4, -20);
            group.add(body);
            const headGeo = new THREE.SphereGeometry(1);
            const head = new THREE.Mesh(headGeo, bodyMat);
            head.position.set(12, 10, -20);
            group.add(head);
        } else if (city === 'Токио') {
            // Tokyo Tower
            const towerGeo = new THREE.ConeGeometry(1.2, 25, 8);
            const towerMat = new THREE.MeshStandardMaterial({ color: 0xcc6666 });
            const tower = new THREE.Mesh(towerGeo, towerMat);
            tower.position.set(-5, 10, -20);
            group.add(tower);
            // Cherry blossoms (pink spheres)
            for (let i=0; i<20; i++) {
                const blossom = new THREE.SphereGeometry(0.3, 4);
                const bMat = new THREE.MeshStandardMaterial({ color: 0xffaabb });
                const b = new THREE.Mesh(blossom, bMat);
                b.position.set(-5 + (Math.random()-0.5)*6, 15 + Math.random()*5, -20 + (Math.random()-0.5)*6);
                group.add(b);
            }
        } else if (city === 'Рио-де-Жанейро') {
            // Christ the Redeemer (simple cross)
            const crossV = new THREE.BoxGeometry(0.8, 14, 0.8);
            const crossH = new THREE.BoxGeometry(6, 0.8, 0.8);
            const crossMat = new THREE.MeshStandardMaterial({ color: 0xaaaaaa });
            const v = new THREE.Mesh(crossV, crossMat);
            v.position.set(0, 5, -20);
            group.add(v);
            const h = new THREE.Mesh(crossH, crossMat);
            h.position.set(0, 9, -20);
            group.add(h);
            // Sugarloaf mountain (cone)
            const mountain = new THREE.ConeGeometry(8, 12, 8);
            const mountMat = new THREE.MeshStandardMaterial({ color: 0x6a6a6a });
            const m = new THREE.Mesh(mountain, mountMat);
            m.position.set(10, 4, -30);
            group.add(m);
        } else if (city === 'Кейптаун') {
            // Table Mountain (flat top)
            const plateau = new THREE.BoxGeometry(20, 5, 10);
            const platMat = new THREE.MeshStandardMaterial({ color: 0x7a7a7a });
            const p = new THREE.Mesh(plateau, platMat);
            p.position.set(0, 2, -30);
            group.add(p);
        } else if (city === 'Сидней') {
            // Opera House sails
            for (let i=0; i<4; i++) {
                const sailGeo = new THREE.ConeGeometry(2, 8, 4);
                const sailMat = new THREE.MeshStandardMaterial({ color: 0xffffff });
                const sail = new THREE.Mesh(sailGeo, sailMat);
                sail.position.set(i*4 - 4, 2, -20);
                sail.rotation.y = 0.3 * i;
                group.add(sail);
            }
        }

        return group;
    }

    // ========== VOLUMETRIC CLOUDS (MULTI-LAYER) ==========
    function createClouds(count) {
        const group = new THREE.Group();
        for (let i = 0; i < count; i++) {
            const cloud = new THREE.Group();
            const layers = 2 + Math.floor(Math.random() * 3);
            for (let l = 0; l < layers; l++) {
                const parts = 3 + Math.floor(Math.random() * 4);
                for (let j = 0; j < parts; j++) {
                    const size = 1.5 + Math.random() * 2.5;
                    const sphere = new THREE.Mesh(
                        new THREE.SphereGeometry(size, 7, 7),
                        new THREE.MeshStandardMaterial({
                            color: 0xeeeeee,
                            emissive: 0x333333,
                            transparent: true,
                            opacity: 0.35 + Math.random()*0.3,
                            map: cloudNoiseTexture
                        })
                    );
                    sphere.position.set((j-1)*2.5, Math.sin(j+l)*1.5, (l-1)*1.5);
                    cloud.add(sphere);
                }
            }
            cloud.position.set((Math.random()-0.5)*100, 20 + Math.random()*30, (Math.random()-0.5)*80);
            cloud.userData = { speed: 0.01 + Math.random()*0.03, dir: Math.random()>0.5 ? 1 : -1 };
            group.add(cloud);
        }
        return group;
    }

    // ========== WEATHER PARTICLES ==========
    function createRain(count) {
        const geom = new THREE.BufferGeometry();
        const pos = new Float32Array(count * 3);
        const speeds = new Float32Array(count);
        for (let i = 0; i < count; i++) {
            pos[i*3] = (Math.random() - 0.5) * 160;
            pos[i*3+1] = Math.random() * 80;
            pos[i*3+2] = (Math.random() - 0.5) * 120;
            speeds[i] = 0.4 + Math.random() * 0.5;
        }
        geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        geom.setAttribute('speed', new THREE.BufferAttribute(speeds, 1));
        const mat = new THREE.PointsMaterial({ color: 0xaaccff, map: rainTexture, size: 0.3, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
        const rain = new THREE.Points(geom, mat);
        rain.userData = { type: 'rain' };
        return rain;
    }

    function createSnow(count) {
        const geom = new THREE.BufferGeometry();
        const pos = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) {
            pos[i*3] = (Math.random() - 0.5) * 160;
            pos[i*3+1] = Math.random() * 80;
            pos[i*3+2] = (Math.random() - 0.5) * 120;
        }
        geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        const mat = new THREE.PointsMaterial({ color: 0xffffff, map: snowTexture, size: 0.4, transparent: true, blending: THREE.NormalBlending, depthWrite: false });
        const snow = new THREE.Points(geom, mat);
        snow.userData = { type: 'snow' };
        return snow;
    }

    // ========== FADING CITY LABEL WITH DISINTEGRATION EFFECT ==========
    function createCityLabel(city) {
        const canvas = document.createElement('canvas');
        canvas.width = 1024;
        canvas.height = 256;
        const ctx = canvas.getContext('2d');
        // Semi-transparent background to ensure readability
        ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.font = 'Bold 120px "Inter", sans-serif';
        ctx.fillStyle = '#ff6600';
        ctx.shadowColor = '#ff6600';
        ctx.shadowBlur = 30;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(city, canvas.width/2, canvas.height/2);
        const texture = new THREE.CanvasTexture(canvas);
        const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false, depthWrite: false });
        const sprite = new THREE.Sprite(material);
        sprite.scale.set(18, 4.5, 1);
        sprite.position.set(0, 30, 0);
        // Store data for disintegration effect
        sprite.userData = { 
            alpha: 1.0,
            disintegrate: false,
            particles: [] // will hold particles if we want to explode
        };
        return sprite;
    }

    // ========== LIGHTNING ==========
    function createLightning() {
        const points = [];
        let x = (Math.random()-0.5)*50;
        let y = 40;
        let z = (Math.random()-0.5)*40;
        for (let i=0; i<18; i++) {
            points.push(new THREE.Vector3(x, y, z));
            x += (Math.random()-0.5)*8;
            y -= 3 + Math.random()*6;
            z += (Math.random()-0.5)*5;
        }
        const geom = new THREE.BufferGeometry().setFromPoints(points);
        const line = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: 0xffdd88 }));
        scene.add(line);
        setTimeout(() => scene.remove(line), 150);
        const flashLight = new THREE.PointLight(0xffaa88, 5, 60);
        flashLight.position.set(points[0].x, points[0].y, points[0].z);
        scene.add(flashLight);
        setTimeout(() => scene.remove(flashLight), 120);
    }

    // ========== CLEAR SCENE (keep only lights and ground) ==========
    function clearScene() {
        while(scene.children.length) scene.remove(scene.children[0]);
        scene.add(ambientLight);
        scene.add(sunLight);
        scene.add(backLight);
        scene.add(sunMesh);
        // ground
        const groundGeo = new THREE.CircleGeometry(120, 128);
        const groundMat = new THREE.MeshStandardMaterial({ color: 0x2a5a2a });
        const ground = new THREE.Mesh(groundGeo, groundMat);
        ground.rotation.x = -Math.PI/2;
        ground.position.y = -2;
        ground.receiveShadow = true;
        scene.add(ground);
        // nature (trees, grass) - we need to re-add
        if (natureGroup) scene.add(natureGroup);
        particles = [];
    }

    // ========== ANIMATION LOOP ==========
    function animate() {
        if (!overlay.classList.contains('active')) return;
        const delta = clock.getDelta();

        // Sun movement
        sunMesh.rotation.y += 0.002;
        sunMesh.position.x = 25 + Math.sin(Date.now()*0.001)*5;
        sunLight.position.copy(sunMesh.position);

        // Cloud movement
        scene.children.forEach(child => {
            if (child.isGroup && child.userData.speed) {
                child.position.x += child.userData.dir * child.userData.speed * delta * 30;
                if (child.position.x > 80) child.position.x = -80;
                if (child.position.x < -80) child.position.x = 80;
            }
        });

        // Fade and disintegrate city label
        scene.children.forEach(child => {
            if (child.isSprite && child.userData.alpha !== undefined) {
                child.material.opacity = child.userData.alpha;
                child.userData.alpha -= 0.0025;
                if (child.userData.alpha <= 0) {
                    // Disintegration effect: create particles from label position
                    for (let i=0; i<200; i++) {
                        const partGeo = new THREE.BoxGeometry(0.2,0.2,0.2);
                        const partMat = new THREE.MeshStandardMaterial({ color: 0xff6600, emissive: 0x331100 });
                        const part = new THREE.Mesh(partGeo, partMat);
                        part.position.copy(child.position);
                        part.userData = {
                            vel: new THREE.Vector3((Math.random()-0.5)*0.5, Math.random()*0.5, (Math.random()-0.5)*0.5),
                            life: 1.0
                        };
                        scene.add(part);
                        // store in array for animation (but we'll handle in this loop for simplicity)
                        // we'll just update in this same animate function
                        child.userData.particles.push(part);
                    }
                    scene.remove(child);
                }
            }
            // Update disintegration particles
            if (child.userData && child.userData.vel) {
                child.position.add(child.userData.vel);
                child.userData.life -= 0.01;
                child.material.opacity = child.userData.life;
                if (child.userData.life <= 0) scene.remove(child);
            }
        });

        // Weather particles animation
        particles.forEach(p => {
            const pos = p.geometry.attributes.position.array;
            if (p.userData.type === 'rain') {
                for (let i = 1; i < pos.length; i+=3) {
                    pos[i] -= 0.5 * delta * 60; // fall speed
                    // wind
                    pos[i-1] += Math.sin(Date.now()*0.002 + pos[i]) * 0.2;
                    if (pos[i] < -10) {
                        pos[i] = 70;
                        pos[i-1] = (Math.random()-0.5)*160;
                        pos[i+1] = (Math.random()-0.5)*120;
                    }
                }
            } else if (p.userData.type === 'snow') {
                for (let i = 1; i < pos.length; i+=3) {
                    pos[i] -= 0.15 * delta * 60;
                    pos[i-1] += Math.sin(Date.now()*0.001 + pos[i]) * 0.1;
                    if (pos[i] < -10) {
                        pos[i] = 70;
                        pos[i-1] = (Math.random()-0.5)*160;
                        pos[i+1] = (Math.random()-0.5)*120;
                    }
                }
            }
            p.geometry.attributes.position.needsUpdate = true;
        });

        // Lightning
        if (currentType === 'stormy' && Math.random() < LIGHTNING_CHANCE) {
            createLightning();
        }

        composer.render();
        animationFrame = requestAnimationFrame(animate);
    }

    // ========== PUBLIC API ==========
    window.showWeatherScene = function(city, type, temp) {
        clearScene();
        currentCity = city;
        currentType = type;
        currentTemp = temp;

        // Set background and fog based on weather
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

        // Add weather particles
        if (type === 'rainy' || type === 'stormy') {
            const rain = createRain(RAIN_COUNT);
            scene.add(rain);
            particles.push(rain);
        }
        if (type === 'snowy') {
            const snow = createSnow(SNOW_COUNT);
            scene.add(snow);
            particles.push(snow);
        }

        // Add clouds (except for sunny)
        if (type !== 'sunny') {
            const clouds = createClouds(CLOUD_COUNT);
            scene.add(clouds);
        }

        // Add city silhouette
        cityGroup = createCity(city);
        scene.add(cityGroup);

        // Add city label (will fade and disintegrate)
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