"""GitHub Actions Secrets를 이용해 본인 이메일로 시험 메시지 발송."""
import sys
from check_notices import email_settings, send_email, POSTAL_URL

def main():
    settings = email_settings()
    if not all(settings.values()):
        sys.exit('설정이 없습니다: SMTP_USERNAME, SMTP_PASSWORD, ALERT_EMAIL_TO')
    send_email({'title': '알림 연결 테스트 (실제 채용공고 아님)',
                'date': '테스트', 'url': POSTAL_URL, 'source': '우정사업본부',
                'sources': ['우정사업본부']}, settings)
    print('테스트 이메일을 발송했습니다.')


if __name__ == '__main__':
    main()
