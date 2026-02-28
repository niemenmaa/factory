FROM factory-agent:base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      lsb-release apt-transport-https && \
    curl -sSLo /tmp/debsuryorg-archive-keyring.deb \
      https://packages.sury.org/debsuryorg-archive-keyring.deb && \
    dpkg -i /tmp/debsuryorg-archive-keyring.deb && \
    echo "deb [signed-by=/usr/share/keyrings/deb.sury.org-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" \
      | tee /etc/apt/sources.list.d/sury-php.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      php8.4-cli php8.4-xml php8.4-mbstring php8.4-curl \
      php8.4-zip php8.4-sqlite3 php8.4-mysql unzip && \
    rm -rf /var/lib/apt/lists/* /tmp/*

RUN curl -sS https://getcomposer.org/installer | php -- \
      --install-dir=/usr/local/bin --filename=composer
