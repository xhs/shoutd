FROM ubuntu:latest
MAINTAINER Xiaohan Song <chef@dark.kitchen>

RUN apt-get update && apt-get install -y \
    iproute2 python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install --upgrade pip
RUN pip3 install structlog pyroute2 docker-py netifaces

ADD . /code
WORKDIR /code
RUN python3 setup.py install

VOLUME ["/usr/lib/docker/plugins"]
EXPOSE 7788

ENTRYPOINT ["shoutd"]
