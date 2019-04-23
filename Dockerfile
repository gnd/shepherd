FROM python:3.5.3

WORKDIR /app

COPY requirements.txt /app

RUN pip install -r requirements.txt

COPY . /app

RUN python setup.py install

CMD python -u app.py

ENV REDIS_BROWSER_URL redis://redis/0
ENV IDLE_TIMEOUT 60
ENV BROWSER_NET shepherd_default
ENV WEBRTC_HOST_IP 127.0.0.1
 
