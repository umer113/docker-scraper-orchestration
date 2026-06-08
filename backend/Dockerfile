FROM selenium/standalone-chrome:latest

USER root

RUN apt-get update && apt-get install -y python3 python3-pip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /appy

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY scraper.py .

CMD ["/bin/bash", "-c", "/opt/bin/entry_point.sh & sleep 5 && python3 scraper.py"]