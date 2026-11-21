import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.willen.remotemic"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.willen.remotemic"
        minSdk = 26
        targetSdk = 35
        versionCode = 11
        versionName = "1.2.8"
    }

    // release 签名凭据放在仓库根 signing/keystore.properties（连同 keystore 一起被
    // .gitignore 屏蔽，不进公开仓库）。换机器/换项目把整个 signing/ 拷过去即可复用同一签名。
    // 注意 Gradle 的 rootProject 是 android-app/，仓库根要再往上一层。
    val signingPropsFile = rootProject.file("../signing/keystore.properties")
    val signingProps = Properties().apply {
        if (signingPropsFile.exists()) signingPropsFile.inputStream().use { load(it) }
    }

    signingConfigs {
        create("release") {
            if (signingPropsFile.exists()) {
                storeFile = rootProject.file("../${signingProps.getProperty("storeFile")}")
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // 有 signing/ 就用独立 release 签名；没有则回退 debug 并大声告警——
            // 回退包只能自用覆盖安装，**绝不能当成 release 发出去**（签名与正式包不同）。
            if (signingPropsFile.exists()) {
                signingConfig = signingConfigs.getByName("release")
            } else {
                logger.warn("⚠ 缺少 signing/keystore.properties，release 回退 debug 签名，禁止对外发布！")
                signingConfig = signingConfigs.getByName("debug")
            }
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // 扫码：CameraX 取流 + ZXing core 解码（都是开源 Apache-2.0），自建竖屏扫码页
    implementation("androidx.camera:camera-core:1.4.2")
    implementation("androidx.camera:camera-camera2:1.4.2")
    implementation("androidx.camera:camera-lifecycle:1.4.2")
    implementation("androidx.camera:camera-view:1.4.2")
    implementation("com.google.zxing:core:3.5.3")
}
