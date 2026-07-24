(function() {
    'use strict';

    const CONSENT_COOKIE = 'cookie_consent';
    const CONSENT_EXPIRY_DAYS = 365;

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    function setCookie(name, value, days) {
        const expires = new Date(Date.now() + days * 864e5).toUTCString();
        document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax; Secure`;
    }

    function hasConsent() {
        return getCookie(CONSENT_COOKIE) === 'accepted';
    }

    function hasDeclined() {
        return getCookie(CONSENT_COOKIE) === 'declined';
    }

    function updateConsentMode(granted) {
        if (typeof window.gtag === 'function') {
            window.gtag('consent', 'update', {
                analytics_storage: granted ? 'granted' : 'denied',
                ad_storage: granted ? 'granted' : 'denied',
                ad_user_data: granted ? 'granted' : 'denied',
                ad_personalization: granted ? 'granted' : 'denied'
            });
        }
    }

    function showBanner() {
        if (hasConsent()) {
            updateConsentMode(true);
            return;
        }

        if (hasDeclined()) {
            updateConsentMode(false);
            return;
        }

        const privacyUrl = document.body.dataset.privacyUrl || '/privacy';

        const banner = document.createElement('div');
        banner.id = 'cookie-consent-banner';
        banner.setAttribute('role', 'dialog');
        banner.setAttribute('aria-live', 'polite');
        banner.setAttribute('aria-label', 'Cookie consent');
        banner.innerHTML = `
            <div class="cookie-consent-content">
                <p class="cookie-consent-text">
                    We use cookies to enhance your experience and analyze traffic. 
                    By clicking "Accept", you consent to our use of analytics cookies (Google Analytics).
                    You can change your preferences anytime.
                </p>
                <div class="cookie-consent-buttons">
                    <button type="button" class="btn btn-primary cookie-accept" aria-label="Accept analytics cookies">
                        Accept
                    </button>
                    <button type="button" class="btn btn-outline-secondary cookie-decline" aria-label="Decline analytics cookies">
                        Decline
                    </button>
                    <a href="${privacyUrl}" class="btn btn-link cookie-policy" aria-label="Read privacy policy">
                        Privacy Policy
                    </a>
                </div>
            </div>
        `;

        document.body.appendChild(banner);

        banner.querySelector('.cookie-accept').addEventListener('click', () => {
            setCookie(CONSENT_COOKIE, 'accepted', CONSENT_EXPIRY_DAYS);
            banner.remove();
            updateConsentMode(true);
        });

        banner.querySelector('.cookie-decline').addEventListener('click', () => {
            setCookie(CONSENT_COOKIE, 'declined', CONSENT_EXPIRY_DAYS);
            banner.remove();
            updateConsentMode(false);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showBanner);
    } else {
        showBanner();
    }

    window.cookieConsent = {
        accept: () => {
            setCookie(CONSENT_COOKIE, 'accepted', CONSENT_EXPIRY_DAYS);
            updateConsentMode(true);
        },
        decline: () => {
            setCookie(CONSENT_COOKIE, 'declined', CONSENT_EXPIRY_DAYS);
            updateConsentMode(false);
        },
        reset: () => {
            document.cookie = `${CONSENT_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
            location.reload();
        }
    };
})();